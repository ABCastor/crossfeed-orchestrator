"""Seeded extraction documents with explicit authority and correction rules."""
from datetime import datetime, timedelta, timezone
import json

TEMPLATE_IDS = ('purchase_order', 'shipping_manifest', 'experiment_report', 'incident_chronology')
TIERS = ('easy', 'medium', 'hard', 'expert')


def _schema(expected):
    types = {str: 'string', int: 'integer', bool: 'boolean', type(None): 'null', list: 'array'}
    properties = {}
    for key, value in expected.items():
        constraint = {'type': types[type(value)]}
        if isinstance(value, list):
            constraint['items'] = {'type': 'string'}
        properties[key] = constraint
    return {'type': 'object', 'properties': properties, 'required': list(expected), 'additionalProperties': True}


def _pack(topic, rules, document, expected):
    prompt = ('Extract the %s record from document.txt. %s\n'
              'Return only one JSON object matching the schema. Preserve unknown values as JSON null '
              'where the schema permits null. Do not guess or copy provisional records.\n'
              'JSON Schema: %s\n' % (topic, rules, json.dumps(_schema(expected), sort_keys=True)))
    workspace = {'document.txt': document}
    return prompt, workspace, {'kind': 'extraction', 'expected': expected}, dict(workspace), json.dumps(expected) + '\n'


def _orders(rng, tier):
    order = 'PO-%d' % rng.randrange(10000, 99999)
    customer = rng.choice(('Northstar Labs', 'Willow Studio', 'Cedar Workshop', 'Harbor Supply'))
    quantity = rng.randrange(4, 36)
    price = rng.randrange(1200, 8500)
    expedited = bool(rng.randrange(2))
    tags = sorted(rng.sample(('fragile', 'priority', 'reusable', 'indoor'), 2))
    expected = {'order_id': order, 'customer': customer, 'quantity': quantity}
    rows = ['PURCHASE ORDER CORRESPONDENCE', 'Scope: only order %s, item filter cartridge.' % order,
            'Accepted order, operations signature R1:', 'order_id: %s' % order,
            'customer: %s' % customer, 'quantity: %d cartridges' % (quantity if tier == 'easy' else quantity + 2),
            'Other order PO-00000: customer Example Demo; quantity 99; do not combine orders.']
    rules = ('Use only the scoped order and item. Accepted operations-signed records outrank drafts; '
             'apply accepted revisions in increasing R-number order. A revision changes only its named fields. '
             'Quantity counts cartridges, not packages. Customer names are literal.')
    if tier != 'easy':
        expected.update(unit_price_cents=price, shipping=None, expedited=expedited, tags=tags)
        rows += ['R1 continued: unit price USD %d.%02d; shipping charge pending, no numeric amount confirmed.' % divmod(price + 75, 100),
                 'R1 continued: expedited=no; tags=indoor.',
                 'Draft R99 from sales, unsigned: quantity 800; unit price USD 0.01; expedited=yes.',
                 'Accepted operations signature R2: quantity=%d cartridges; unit price=USD %d.%02d; '
                 'expedited=%s; tags=%s.' % (quantity, price // 100, price % 100,
                                             'yes' if expedited else 'no', ','.join(tags))]
        rules += (' unit_price_cents is the per-cartridge USD price in integer cents. Shipping remains null '
                  'until an accepted numeric charge exists; pending is not zero. Convert yes/no to booleans. '
                  'Tags are distinct exact labels sorted alphabetically.')
    if tier in ('hard', 'expert'):
        initial = quantity + 2
        cartons = rng.randrange(2, 5)
        each = rng.randrange(3, 8)
        removed = cartons * each + initial - quantity
        rows = [row for row in rows if not row.startswith('Accepted operations signature R2:')]
        rows += ['Accepted operations signature R2: add %d cartons, %d cartridges per carton; other fields unchanged.' % (cartons, each),
                 'Accepted operations signature R3: remove %d individual cartridges; corrected unit price '
                 'USD %d.%02d; expedited=%s; replace all tags with %s.' %
                 (removed, price // 100, price % 100, 'yes' if expedited else 'no', ','.join(tags)),
                 'Later warehouse note, unsigned: R3 probably means remove cartons. This note has no authority.']
    if tier == 'expert':
        per_case = rng.randrange(3, 8)
        cases = rng.randrange(2, 6)
        shortage = rng.randrange(1, per_case)
        quantity = cases * per_case - shortage
        expected['quantity'] = quantity
        final_price = price + rng.randrange(20, 160)
        expected['unit_price_cents'] = final_price
        rows += ['Accepted operations signature R4: reset quantity to %d cases. Use the packing specification referenced below.' % cases,
                 'Packing specification P7, accepted: each case contains %d cartridges. Historical P6 had %d.' % (per_case, per_case + 2),
                 'Accepted operations signature R5: short-pick %d cartridges after the R4 reset.' % shortage,
                 'Accepted operations signature R6: VOID the R3 price correction only. Restore the R1 price; '
                 'R3 expedited and tag fields remain accepted.',
                 'Accepted operations signature R7: replace restored price with USD %d.%02d per cartridge, '
                 'net of discount; do not subtract the discount again.' % divmod(final_price, 100),
                 'Accepted operations signature R8: shipping is still pending; quantity and all other fields unchanged.']
        for revision in range(12):
            rows += ['Unaccepted sales annex D%d for %s: suggested quantity %d cases; suggested price USD %d.%02d; '
                     'approval absent. Scope and authority remain the accepted R records.' %
                     (revision, 'PO-00000' if revision % 2 else order, rng.randrange(10, 80),
                      rng.randrange(20, 90), rng.randrange(100))]
    return _pack('purchase order', rules, '\n'.join(rows) + '\n', expected)


def _manifests(rng, tier):
    shipment = 'SHIP-%d' % rng.randrange(10000, 99999)
    destination = rng.choice(('Dock C', 'Warehouse North', 'Research Receiving', 'Depot West'))
    count = (2, 4, 6, 10)[TIERS.index(tier)]
    gross = [rng.randrange(1000, 9000) for _ in range(count)]
    tare = [rng.randrange(80, 600) for _ in range(count)]
    rules = ('Use only the target shipment. Signed inspection revisions outrank the original manifest and unsigned '
             'carrier scans. The greatest signed revision number for each package wins, regardless of page order. '
             'A replacement supplies the complete package record; VOID removes that package. '
             'Convert kg to grams by multiplying by 1000 and round each converted mass to the nearest integer gram, '
             'with an exact half rounded upward. gross_grams sums gross mass; net_grams sums gross minus tare. '
             'package_count counts surviving package IDs, not scan rows. item_codes are distinct surviving codes sorted alphabetically.')
    rows = ['SHIPPING MANIFEST', 'Target shipment: %s; destination: %s.' % (shipment, destination),
            'Revision 0 is the original manifest, accepted unless a signed inspection replaces it.']
    ids = ['PK-%d' % (i + 1) for i in range(count)]
    codes = [rng.choice(('SENSOR', 'FILTER', 'BRACKET', 'CABLE')) for _ in range(count)]
    for index, package in enumerate(ids):
        rows.append('Manifest r0 shipment=%s package=%s code=%s gross=%d g tare=%d g.' %
                    (shipment, package, codes[index], gross[index], tare[index]))
    active = list(range(count))
    if tier != 'easy':
        gross[0] += rng.randrange(100, 900)
        rows += ['SIGNED inspection r2 shipment=%s package=%s code=%s gross=%.3f kg tare=%d g REPLACE.' %
                 (shipment, ids[0], codes[0], gross[0] / 1000, tare[0]),
                 'UNSIGNED scan r99 shipment=%s package=%s gross=999 kg tare=0 g; not an inspection.' % (shipment, ids[0])]
    if tier in ('hard', 'expert'):
        active.remove(count - 1)
        rows += ['SIGNED inspection r3 shipment=%s package=%s VOID; lost before dispatch.' % (shipment, ids[-1]),
                 'Duplicate scan: shipment=%s package=%s gross=%d g; this is not an additional package.' % (shipment, ids[1], gross[1]),
                 'Other shipment SHIP-00000: package PK-900 gross=45 kg tare=2 kg.']
    if tier == 'expert':
        for index in range(1, count - 1):
            corrected_gross = gross[index] + rng.randrange(20, 200)
            corrected_tare = tare[index] + rng.randrange(5, 60)
            # A half-gram input must round upward before aggregation.
            kg_text = '%d.%04d' % (corrected_gross // 1000, (corrected_gross % 1000) * 10 + 5)
            rows += ['SIGNED inspection r%d shipment=%s package=%s code=%s gross=%s kg tare=%d g REPLACE.' %
                     (10 + index, shipment, ids[index], codes[index], kg_text, corrected_tare),
                     'SIGNED inspection r1 shipment=%s package=%s code=%s gross=%d g tare=%d g REPLACE. '
                     'This older signed revision arrived late.' % (shipment, ids[index], codes[index], gross[index], tare[index])]
            gross[index] = corrected_gross + 1
            tare[index] = corrected_tare
        rows += ['SIGNED inspection r40 shipment=%s package=%s code=%s gross=%d g tare=%d g REPLACE; '
                 'reinstates the previously voided package.' % (shipment, ids[-1], codes[-1], gross[-1], tare[-1])]
        active.append(count - 1)
        for index in range(16):
            rows.append('Unsigned forwarding note %d: carrier estimated total %d kg including pallet and '
                        'other shipments; this is not package evidence.' % (index, rng.randrange(20, 80)))
    expected = {'shipment_id': shipment, 'destination': destination, 'package_count': len(active),
                'gross_grams': sum(gross[index] for index in active),
                'net_grams': sum(gross[index] - tare[index] for index in active),
                'item_codes': sorted(set(codes[index] for index in active))}
    return _pack('shipping manifest', rules, '\n'.join(rows) + '\n', expected)


def _experiments(rng, tier):
    experiment = 'EXP-%d' % rng.randrange(10000, 99999)
    sample = 'S-%d' % rng.randrange(100, 999)
    mass = rng.randrange(500, 6000)
    concentration = rng.randrange(60, 200) * 2
    volume = rng.randrange(1000, 8000)
    rows = ['EXPERIMENT MEASUREMENT REPORT', 'Scope: experiment %s, sample %s only.' % (experiment, sample),
            'Authority: laboratory-signed revisions override earlier laboratory-signed values for the '
            'specified fields. Unsigned instrument exports, drafts, and records for other samples have no authority.',
            'Laboratory-signed R1: sample mass=%d mg; concentration=%d micrograms/mL; '
            'delivered volume=%d microliters; approved=yes.' % (mass, concentration, volume),
            'Other sample S-000 in experiment %s: mass=80 g; concentration=99 mg/L; volume=20 mL.' % experiment]
    rules = ('Use only the scoped sample and laboratory-signed records. Apply signed revision numbers in increasing '
             'order, updating only specified fields; a VOID instruction cancels only the named correction. '
             'mass_mg is sample mass after tare. 1 g=1000 mg, 1 mL=1000 microliters, '
             '1 mg/L=1 microgram/mL=1000 nanograms/mL. concentration_ng_per_ml is final calibrated concentration. '
             'dose_ng equals concentration_ng_per_ml multiplied by delivered volume_ul and divided by 1000. '
             'Round only the final integer fields to nearest, exact half upward. approved uses yes/no; reviewer '
             'is null unless explicitly named by an authoritative record.')
    if tier != 'easy':
        volume += rng.randrange(100, 800)
        rows += ['Unsigned export R90: delivered volume=50 mL; concentration=999 micrograms/mL.',
                 'Laboratory-signed R2: replace delivered volume with %d.%03d mL, not an increment.' % divmod(volume, 1000)]
    if tier in ('hard', 'expert'):
        tare = rng.randrange(100, 700)
        blank = rng.randrange(5, 25)
        raw = concentration
        concentration = raw * 2 - blank
        rows += ['Laboratory-signed R3: replace direct concentration with RAW instrument result %d mg/L. '
                 'Calibration applies to raw results only.' % raw,
                 'Laboratory-signed calibration C2 for this experiment: calibrated micrograms/mL '
                 '= raw micrograms/mL * 2 - blank; blank=%d micrograms/mL.' % blank,
                 'Laboratory-signed R4: measured gross mass=%d.%03d g; tare=%d mg. This replaces R1 sample mass.' %
                 ((mass + tare) // 1000, (mass + tare) % 1000, tare),
                 'Calibration C99 draft: multiply by 10. Unsigned, never adopted.']
    if tier == 'expert':
        final_raw = raw + 2 * rng.randrange(5, 20)
        blank += rng.randrange(1, 7)
        concentration = final_raw * 3 // 2 - blank
        dead_volume = rng.randrange(20, 120)
        withdraw = volume + rng.randrange(200, 900)
        volume = withdraw - dead_volume
        rows += ['Laboratory-signed R5: VOID the R3 raw-result correction only; restore R1 concentration temporarily.',
                 'Laboratory-signed R6: replace concentration with RAW result %d micrograms/mL; '
                 'use the greatest adopted C-number calibration, not the calibration nearest on the page.' % final_raw,
                 'Laboratory-signed adopted calibration C4: gain=3/2, blank=%d mg/L; '
                 'calibrated concentration = raw * gain - blank. Supersedes C2 in full.' % blank,
                 'Laboratory-signed R7: withdraw %d.%03d mL; instrument dead volume=%d microliters. '
                 'Replace delivered volume with withdrawal minus dead volume. No spill occurred.' %
                 (withdraw // 1000, withdraw % 1000, dead_volume),
                 'Laboratory-signed R8: reviewer not recorded; approved=yes. All other fields unchanged.',
                 'Laboratory-signed calibration C1, forwarded later: gain=1; blank=0. The smaller C-number loses.']
        for index in range(16):
            rows.append('Unsigned acquisition row %d for %s: raw=%d mg/L, gain=%d, volume=%d mL; '
                        'these unreviewed readings have not been laboratory-signed.' %
                        (index, sample if index % 3 else 'S-000', rng.randrange(100, 900),
                         rng.randrange(2, 10), rng.randrange(10, 50)))
    expected = {'experiment_id': experiment, 'sample_id': sample, 'mass_mg': mass,
                'concentration_ng_per_ml': concentration * 1000, 'volume_ul': volume,
                'dose_ng': concentration * volume, 'approved': True, 'reviewer': None}
    return _pack('experiment result', rules, '\n'.join(rows) + '\n', expected)


def _timestamp(moment, offset_hours=0):
    return moment.astimezone(timezone(timedelta(hours=offset_hours))).isoformat(timespec='seconds')


def _incidents(rng, tier):
    incident = 'INC-%d' % rng.randrange(10000, 99999)
    base = datetime(2026, 7, rng.randrange(1, 25), rng.randrange(0, 18), rng.randrange(0, 60), tzinfo=timezone.utc)
    durations = [rng.randrange(240, 1800) for _ in range((1, 2, 3, 5)[TIERS.index(tier)])]
    gap = rng.randrange(300, 900)
    services = sorted(rng.sample(('api', 'checkout', 'search', 'web', 'worker'), 2))
    severity = rng.choice(('SEV1', 'SEV2', 'SEV3'))
    windows = []
    cursor = base
    rows = ['INCIDENT COMMUNICATIONS', 'Scope: incident %s; only customer-impact windows count.' % incident,
            'Incident-commander accepted summary: severity=%s; affected services=%s; customer impact=yes.' %
            (severity, ','.join(services)),
            'Window authority: the greatest accepted incident-commander revision per window ID wins. '
            'VOID removes a window. Draft updates and other incidents are not accepted. Each replacement '
            'provides complete start/end/kind fields. Timestamps include their own UTC offsets.']
    for index, duration in enumerate(durations):
        end = cursor + timedelta(seconds=duration)
        windows.append([cursor, end])
        offset = rng.choice((-4, 0, 2, 5)) if tier != 'easy' else 0
        rows.append('Commander accepted r0 incident=%s window=W%d start=%s end=%s kind=customer-impact.' %
                    (incident, index, _timestamp(cursor, offset), _timestamp(end, offset)))
        cursor = end + timedelta(seconds=gap)
    rules = ('Use the scoped incident and accepted commander records. For each window, greatest revision number '
             'wins even if an older record appears later. Count only surviving kind=customer-impact windows. '
             'Normalize all timestamps to UTC with the format YYYY-MM-DDTHH:MM:SSZ. started_utc is the earliest '
             'counted start and resolved_utc the latest counted end. downtime_seconds is the union duration '
             'of counted half-open [start,end) intervals; exclude recovery gaps and scheduled maintenance. '
             'Service labels are unique and sorted alphabetically. customer_impact uses yes/no. '
             'root_cause remains null unless confirmed explicitly; suspected causes are not confirmed.')
    if tier != 'easy':
        rows += ['Draft status r99 incident=%s: resolved=%s; severity=SEV0; services=all. Not commander accepted.' %
                 (incident, _timestamp(cursor + timedelta(hours=8), 2)),
                 'Other incident INC-00000: customer-impact began yesterday and remains open.',
                 'Commander accepted note: root cause is unconfirmed; suspect cache failure.']
    if tier in ('hard', 'expert'):
        maintenance_start = base - timedelta(hours=1)
        rows += ['Commander accepted r4 incident=%s window=M0 start=%s end=%s kind=scheduled-maintenance.' %
                 (incident, _timestamp(maintenance_start, 2), _timestamp(cursor + timedelta(hours=1), 2)),
                 'Status dashboard estimates total elapsed time from first start to final end. '
                 'It includes recovery gaps and is not the downtime metric.']
    if tier == 'expert':
        # Correct one window, void another, and add an overlap which must not double-count.
        corrected_end = windows[1][1] + timedelta(seconds=rng.randrange(20, gap - 1))
        windows[1][1] = corrected_end
        last_start, last_end = windows[-1]
        windows.pop()
        overlap_start = windows[1][0] + timedelta(seconds=30)
        overlap_end = corrected_end + timedelta(seconds=45)
        windows.append([overlap_start, overlap_end])
        rows += ['Commander accepted r5 incident=%s window=W1 start=%s end=%s kind=customer-impact REPLACE.' %
                 (incident, _timestamp(windows[1][0], -4), _timestamp(corrected_end, 5)),
                 'Commander accepted r6 incident=%s window=W4 VOID; this window belonged to another incident.' % incident,
                 'Commander accepted r7 incident=%s window=R1 start=%s end=%s kind=customer-impact REPLACE.' %
                 (incident, _timestamp(overlap_start, 2), _timestamp(overlap_end, -4)),
                 'Commander accepted r1 incident=%s window=W1 start=%s end=%s kind=customer-impact REPLACE. '
                 'Forwarded late, earlier revision than r5.' %
                 (incident, _timestamp(windows[1][0]), _timestamp(corrected_end - timedelta(seconds=100))),
                 'Draft r80 incident=%s window=W4 start=%s end=%s kind=customer-impact. No approval.' %
                 (incident, _timestamp(last_start), _timestamp(last_end))]
        for index in range(16):
            rows.append('Unaccepted channel update D%d for %s: claimed outage %d minutes, suspected root '
                        'cause %s. Commander has not confirmed this message.' %
                        (index, incident if index % 2 else 'INC-00000', rng.randrange(30, 300),
                         rng.choice(('cache', 'network', 'deployment'))))
    # Integer-second set oracle is independent of a worker's interval-union algorithm.
    occupied = set()
    for start, end in windows:
        occupied.update(range(int((start - base).total_seconds()), int((end - base).total_seconds())))
    first = min(start for start, end in windows)
    last = max(end for start, end in windows)
    expected = {'incident_id': incident, 'severity': severity, 'affected_services': services,
                'started_utc': first.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'resolved_utc': last.strftime('%Y-%m-%dT%H:%M:%SZ'), 'downtime_seconds': len(occupied),
                'customer_impact': True, 'root_cause': None}
    return _pack('incident chronology', rules, '\n'.join(rows) + '\n', expected)


def make(rng, template_index, tier):
    if tier not in TIERS:
        raise ValueError('unknown extraction tier')
    return (_orders, _manifests, _experiments, _incidents)[template_index](rng, tier)
