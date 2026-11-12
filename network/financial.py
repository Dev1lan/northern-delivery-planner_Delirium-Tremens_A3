"""Price previously audited network schedules using explicit external tariffs."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def evaluate_schedule(schedule, financial_parameters):
    from network.adapter import audit_result

    if audit_result(schedule)['schedule_audit']['status'] != 'PASSED':
        raise ValueError('Only audited schedules can be priced')
    currency = financial_parameters.get('currency')
    source = financial_parameters.get('source_status')
    rates = financial_parameters.get('rates')
    if not isinstance(currency, str) or not currency.strip() or not isinstance(source, str) or not source.strip():
        raise ValueError('Specify tariff currency and source_status')
    if not isinstance(rates, dict):
        raise ValueError('Specify explicit rates')
    export = schedule['financial_inputs']
    totals = export['totals']
    hours = Decimal(3600)
    quantities = {'loaded_km': Decimal(str(totals['loaded_km'])), 'empty_km': Decimal(str(totals['empty_km'])),
                  'loading_hour': Decimal(totals['loading_seconds']) / hours,
                  'unloading_hour': Decimal(totals['unloading_seconds']) / hours,
                  'waiting_hour': Decimal(totals['waiting_seconds']) / hours,
                  'tug_busy_hour': sum(r['busy_seconds'] for r in export['resources'] if r['vessel_type'] == 'tug') / hours,
                  'barge_busy_hour': sum(r['busy_seconds'] for r in export['resources'] if r['vessel_type'] == 'barge') / hours}
    if rates.keys() != quantities.keys():
        missing = sorted(quantities.keys() - rates.keys())
        unknown = sorted(rates.keys() - quantities.keys())
        raise ValueError(f'Incomplete tariff model. Missing: {missing}. Unknown: {unknown}')
    charges = {}
    for name, amount in quantities.items():
        try:
            rate = Decimal(str(rates[name]))
        except InvalidOperation as exc:
            raise ValueError(f'Invalid tariff for {name}') from exc
        if not rate.is_finite() or rate < 0:
            raise ValueError(f'Tariff for {name} must be finite and nonnegative')
        charges[name] = (rate * amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {'currency': currency, 'source_status': source, 'rates': rates,
            'cost_by_unit': {name: str(cost) for name, cost in charges.items()},
            'total_cost': str(sum(charges.values(), Decimal('0.00')))}
