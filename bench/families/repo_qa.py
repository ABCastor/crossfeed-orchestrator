"""Seeded repository questions with executable public resolution paths."""
import json
import textwrap

TEMPLATE_IDS = ('config_precedence', 'plugin_dispatch', 'scoped_injection', 'route_features')
TIERS = ('easy', 'medium', 'hard', 'expert')


def _source(code):
    return textwrap.dedent(code).lstrip()


def _json(value):
    return json.dumps(value, indent=2, sort_keys=True) + '\n'


def _handlers(rng):
    names = rng.sample(('cedar', 'maple', 'willow', 'birch', 'ash', 'pine', 'elm', 'oak'), 4)
    suffix = rng.randrange(10000, 99999)
    symbol = 'process_%d' % rng.randrange(10000, 99999)
    modules = ['handlers.%s_%d' % (name, suffix) for name in names]
    files = {'handlers/__init__.py': ''}
    for index, module in enumerate(modules):
        files[module.replace('.', '/') + '.py'] = (
            'def %s(payload):\n    return (%r, payload)\n' % (symbol, names[index]))
    return files, names, modules, symbol


def _pack(rng, tier, topic, files, module, symbol):
    target_size = (12, 20, 30, 44)[TIERS.index(tier)]
    batched = tier in ('hard', 'expert')
    files['README.md'] = (
        'The active application entry is entry.py. All scenario inputs are in scenario.json.\n'
        'Archived files retain historical adapter definitions.\n')
    files['archive/__init__.py'] = ''
    index = 0
    while len(files) < target_size:
        index += 1
        files['archive/release_%02d.py' % index] = (
            '# Historical adapter snapshot.\nRELEASE = %d\n\ndef %s(payload):\n'
            '    return (%r, payload)\n' % (rng.randrange(1000, 9999), symbol, rng.choice(('legacy', 'preview', 'rollback'))))
    answer = module.replace('.', '/') + '.py:' + symbol
    question = ('For the scenario.json event batch, which original implementation function produces the '
                'final accepted result for target_event? Follow preparation/commit attempts, context overrides, '
                'and accepted revision replacement; identify the successful invocation, not an earlier attempt. '
                if batched else 'Starting at entry.py:resolve(), which original implementation function '
                'does it return for the scenario.json inputs? ')
    prompt = (
        'Trace the configured %s in this repository. ' + question + 'Resolve imports, re-exports, '
        'aliases and runtime selection. Give the defining function, not a registry key, alias or resolver. '
        'All required inputs are in the public workspace; no external environment or service is involved. '
        'Do not edit files. End with one JSON line {"answer": "relative/file.py:symbol"}.\n') % topic
    return prompt, files, {'kind': 'repo-qa', 'answer': answer}, dict(files), json.dumps({'answer': answer}) + '\n'


def _event_dispatch(rng, tier, files, modules, symbol, imports, selection, preliminary, final_context):
    """Add real multi-stage invocations, keeping each template's selection semantics."""
    if tier not in ('hard', 'expert'):
        return
    scenario = json.loads(files['scenario.json'])
    target = 'event_%d' % rng.randrange(10000, 99999)
    def event(ident, revision, contexts):
        return {'id': ident, 'revision': revision, 'value': rng.randrange(100, 999), 'attempts': contexts}
    preparation = {'phase': 'prepare', 'context': preliminary}
    commit = {'phase': 'commit', 'context': final_context}
    events = [event('neighbor_%d' % i, 1, [{'phase': 'commit', 'context': preliminary}]) for i in range(3)]
    if tier == 'expert':
        events.insert(1, event(target, 1, [{'phase': 'commit', 'context': preliminary}]))
        events.append(event(target, 3, [preparation, commit]))
        events.append(event(target, 2, [{'phase': 'commit', 'context': preliminary}]))
        events.append(event(target, 4, [preparation]))
        events += [event('neighbor_%d' % i, i, [preparation, commit]) for i in range(3, 10)]
    else:
        events.append(event(target, 1, [preparation, commit]))
    scenario.update(target_event=target, events=events)
    files['scenario.json'] = _json(scenario)
    for module in modules:
        files[module.replace('.', '/') + '.py'] = (
            'def %s(payload):\n'
            "    if payload['phase'] == 'prepare':\n"
            "        return {'accepted': False, 'value': payload['value']}\n"
            "    return {'accepted': True, 'value': payload['value']}\n" % symbol)
    files['execution.py'] = _source('''
        def apply_batch(scenario, select):
            accepted = {}
            for event in scenario['events']:
                current = accepted.get(event['id'])
                if current is not None and current['revision'] >= event['revision']:
                    continue
                for attempt in event['attempts']:
                    context = dict(scenario)
                    context.update(attempt['context'])
                    payload = dict(event, phase=attempt['phase'])
                    try:
                        handler = select(context)
                        outcome = handler(payload)
                    except (KeyError, ValueError):
                        continue
                    if outcome['accepted']:
                        accepted[event['id']] = {'revision': event['revision'], 'outcome': outcome}
                        break
            return accepted
    ''')
    files['entry.py'] = (
        'import json\nfrom pathlib import Path\nfrom execution import apply_batch\n' + imports +
        '\n\ndef select(context):\n    return ' + selection + '\n\n'
        "def process_batch():\n    scenario = json.loads(Path(__file__).with_name('scenario.json').read_text())\n"
        '    return apply_batch(scenario, select)\n')


def _config(rng, tier):
    files, names, modules, symbol = _handlers(rng)
    region = rng.choice(('west', 'north', 'south'))
    profiles = {'base': {'delivery': {'provider': names[0], 'retry': 1}},
                'site': {'delivery': {'provider': names[1]}},
                'deployment': {'delivery': {'provider': None, 'retry': 5}},
                'tenant': {'delivery': {'provider': names[3], 'locked': bool(rng.randrange(2))}},
                'preview': {'delivery': {'provider': names[0], 'locked': True}},
                'force': {region: names[2], 'unused': names[0]}}
    if tier == 'expert':
        profiles['regional'] = {'extends': ['base'], 'delivery': {'provider': names[3]}}
        profiles['compliance'] = {'extends': ['limits'], 'delivery': {'provider': None,
                                                                           'locked': profiles['tenant']['delivery']['locked']}}
        profiles['limits'] = {'delivery': {'retry': 9}}
        profiles['tenant'] = {'extends': ['regional', 'compliance'], 'delivery': {'provider': None}}
        profiles['staging'] = {'extends': ['site', 'preview'], 'delivery': {'retry': 99}}
    files['profiles.json'] = _json(profiles)
    files['scenario.json'] = _json({'region': region, 'profile': 'tenant', 'operation': 'delivery'})
    settings_header = _source('''
        import json
        from pathlib import Path

        def profiles():
            return json.loads(Path(__file__).with_name('profiles.json').read_text())
    ''')
    if tier == 'easy':
        body = "    config = profiles()\n    return config['site']['delivery']['provider'] or config['base']['delivery']['provider']\n"
        selected = 1
    elif tier == 'medium':
        body = ("    config = profiles()\n    provider = config['base']['delivery']['provider']\n"
                "    for layer in ('site', 'deployment'):\n"
                "        value = config[layer]['delivery'].get('provider')\n"
                "        if value is not None: provider = value\n    return provider\n")
        selected = 1
    else:
        settings_header += _source('''

            def merge(base, override):
                result = dict(base)
                for key, value in override.items():
                    if value is None:
                        continue
                    if isinstance(value, dict) and isinstance(result.get(key), dict):
                        result[key] = merge(result[key], value)
                    else:
                        result[key] = value
                return result
        ''')
        if tier == 'expert':
            settings_header += _source('''

                def inherited(config, name):
                    layer = config[name]
                    result = {}
                    for parent in layer.get('extends', []):
                        result = merge(result, inherited(config, parent))
                    return merge(result, {key: value for key, value in layer.items() if key != 'extends'})
            ''')
        body = ("    config = profiles()\n    merged = {}\n"
                "    for layer in ('base', 'site', 'deployment', scenario['profile']):\n"
                "        merged = merge(merged, config[layer])\n"
                "    active = merged[scenario['operation']]\n")
        if tier == 'expert':
            body = body.replace('merge(merged, config[layer])', 'merge(merged, inherited(config, layer))')
            body += ("    forced = config['force'].get(scenario['region'])\n"
                     "    if forced is not None and not active.get('locked', False):\n"
                     "        return forced\n")
            selected = 3 if profiles['compliance']['delivery']['locked'] else 2
        else:
            selected = 3
        body += "    return active['provider']\n"
    files['settings.py'] = settings_header + '\ndef select_provider(scenario):\n' + body
    files['registry.py'] = '\n'.join('from %s import %s as adapter_%d' % (module, symbol, i)
                                      for i, module in enumerate(modules)) + '\n\nADAPTERS = {'
    files['registry.py'] += ', '.join('%r: adapter_%d' % (name, i) for i, name in enumerate(names)) + '}\n'
    files['entry.py'] = _source('''
        import json
        from pathlib import Path
        from settings import select_provider
        from registry import ADAPTERS

        def resolve():
            scenario = json.loads(Path(__file__).with_name('scenario.json').read_text())
            return ADAPTERS[select_provider(scenario)]
    ''')
    final_profile = rng.choice(('site', 'tenant')) if tier in ('hard', 'expert') else 'tenant'
    final_context = {'profile': final_profile, 'region': 'no_force'}
    if tier in ('hard', 'expert'):
        selected = 1 if final_profile == 'site' else 3
    _event_dispatch(rng, tier, files, modules, symbol,
                    'from settings import select_provider\nfrom registry import ADAPTERS\n',
                    'ADAPTERS[select_provider(context)]', {'profile': 'site', 'region': 'unused'}, final_context)
    return _pack(rng, tier, 'delivery adapter', files, modules[selected], symbol)


def _plugins(rng, tier):
    files, names, modules, symbol = _handlers(rng)
    capability = rng.choice(('compress', 'encrypt', 'render'))
    tenant = 'tenant_%d' % rng.randrange(100, 999)
    entries = [{'name': name, 'module': module, 'symbol': symbol, 'priority': rng.randrange(10, 50),
                'enabled': True, 'capabilities': [capability], 'tenants': ['*'], 'requires': []}
               for name, module in zip(names, modules)]
    entries[0].update(priority=100, enabled=False)
    entries[1]['priority'] = 60
    entries[2]['priority'] = 80
    entries[3]['priority'] = 90
    selected = 1
    if tier == 'easy':
        entries[2]['enabled'] = entries[3]['enabled'] = False
        rules = "entry['enabled']"
    elif tier == 'medium':
        entries[3]['capabilities'] = ['unrelated']
        rules = "entry['enabled'] and scenario['capability'] in entry['capabilities']"
        selected = 2
    elif tier == 'hard':
        entries[3]['tenants'] = ['different_tenant']
        rules = ("entry['enabled'] and scenario['capability'] in entry['capabilities'] "
                 "and ('*' in entry['tenants'] or scenario['tenant'] in entry['tenants'])")
        selected = 2
    else:
        entries[3]['requires'] = ['unsafe_preview']
        entries[2].update(priority=60, requires=['stable'])
        entries[1]['tenants'] = [tenant]
        entries[2]['tenants'] = ['*']
        rules = ("entry['enabled'] and scenario['capability'] in entry['capabilities'] "
                 "and ('*' in entry['tenants'] or scenario['tenant'] in entry['tenants']) "
                 "and all(scenario['flags'].get(flag, False) for flag in entry['requires'])")
        for index in range(12):
            decoy = {'name': 'candidate_%d' % index, 'module': modules[index % 4], 'symbol': symbol,
                     'priority': 120 + index, 'enabled': True, 'capabilities': [capability],
                     'tenants': ['*'], 'requires': []}
            if index % 4 == 0:
                decoy['enabled'] = False
            elif index % 4 == 1:
                decoy['capabilities'] = ['unrelated']
            elif index % 4 == 2:
                decoy['tenants'] = ['different_tenant']
            else:
                decoy['requires'] = ['unsafe_preview']
            entries.append(decoy)
    rng.shuffle(entries)
    files['plugins.json'] = _json(entries)
    files['scenario.json'] = _json({'capability': capability, 'tenant': tenant, 'flags': {'stable': True, 'unsafe_preview': False}})
    files['catalog.py'] = _source('''
        import json
        from pathlib import Path

        def load_plugins():
            return json.loads(Path(__file__).with_name('plugins.json').read_text())
    ''')
    ranking = "(entry['priority'], scenario['tenant'] in entry['tenants'])" if tier == 'expert' else "entry['priority']"
    files['dispatch.py'] = (
        "from importlib import import_module\nfrom catalog import load_plugins\n\n"
        "def choose(scenario):\n    candidates = [entry for entry in load_plugins() if %s]\n"
        "    chosen = max(candidates, key=lambda entry: %s)\n"
        "    return getattr(import_module(chosen['module']), chosen['symbol'])\n" % (rules, ranking))
    files['entry.py'] = _source('''
        import json
        from pathlib import Path
        from dispatch import choose

        def resolve():
            scenario = json.loads(Path(__file__).with_name('scenario.json').read_text())
            return choose(scenario)
    ''')
    final_context = {'tenant': 'different_tenant'}
    if tier in ('hard', 'expert'):
        selected = 3 if tier == 'hard' else 2
    _event_dispatch(rng, tier, files, modules, symbol, 'from dispatch import choose\n', 'choose(context)',
                    {'tenant': 'different_tenant'}, final_context)
    return _pack(rng, tier, 'plugin dispatch', files, modules[selected], symbol)


def _injection(rng, tier):
    files, names, modules, symbol = _handlers(rng)
    service = rng.choice(('storage', 'notifications', 'reports'))
    scopes = {'root': {'parent': None, 'bindings': {service: {'provider': names[0]},
                                                  'shared': {'provider': names[1]}}},
              'tenant': {'parent': 'root', 'bindings': {'shared': {'alias': 'cache'},
                                                       'cache': {'provider': names[1]}}},
              'request': {'parent': 'tenant', 'bindings': {'cache': {'provider': names[2]}, service: None}},
              'preview': {'parent': 'root', 'bindings': {service: {'provider': names[3]}}}}
    selected = 0
    active_scope = 'root'
    if tier != 'easy':
        scopes['root']['bindings'][service] = {'alias': 'shared'}
        selected = 1
    if tier in ('hard', 'expert'):
        active_scope = 'request'
        selected = 2
    if tier == 'expert':
        scopes['request']['bindings']['cache'] = {'alias': 'override'}
        scopes['tenant']['bindings']['override'] = {'alias': 'stage_0'}
        scopes['root']['bindings']['override'] = {'provider': names[3]}
        scopes['preview']['bindings']['shared'] = {'alias': service}
        for index in range(7):
            owner = 'request' if index % 2 else 'tenant'
            scopes[owner]['bindings']['stage_%d' % index] = {'alias': 'stage_%d' % (index + 1)}
            scopes['root']['bindings']['stage_%d' % index] = {'provider': names[3]}
        scopes['tenant']['bindings']['stage_7'] = {'provider': names[2]}
    files['bindings.json'] = _json(scopes)
    files['scenario.json'] = _json({'scope': active_scope, 'service': service})
    files['providers.py'] = '\n'.join('from %s import %s as _import_%d' % (module, symbol, i)
                                       for i, module in enumerate(modules)) + '\n\n'
    files['providers.py'] += '\n'.join('%s = _import_%d' % (name, i) for i, name in enumerate(names)) + '\n'
    if tier == 'easy':
        resolution = "    binding = scopes[scenario['scope']]['bindings'][scenario['service']]\n"
    elif tier == 'medium':
        resolution = ("    bindings = scopes[scenario['scope']]['bindings']\n"
                      "    binding = bindings[scenario['service']]\n"
                      "    while 'alias' in binding: binding = bindings[binding['alias']]\n")
    else:
        resolution = (
            "    def lookup(key):\n        scope = scenario['scope']\n        while scope is not None:\n"
            "            binding = scopes[scope]['bindings'].get(key)\n"
            "            if binding is not None: return binding\n"
            "            scope = scopes[scope]['parent']\n        raise KeyError(key)\n"
            "    binding = lookup(scenario['service'])\n    seen = set()\n"
            "    while 'alias' in binding:\n        key = binding['alias']\n"
            "        if key in seen: raise ValueError('alias cycle')\n"
            "        seen.add(key)\n        binding = lookup(key)\n")
    files['container.py'] = (
        "import json\nfrom pathlib import Path\nimport providers\n\n"
        "def lookup_provider(scenario):\n"
        "    scopes = json.loads(Path(__file__).with_name('bindings.json').read_text())\n" +
        resolution + "    return getattr(providers, binding['provider'])\n")
    files['entry.py'] = _source('''
        import json
        from pathlib import Path
        from container import lookup_provider as build_service

        def resolve():
            scenario = json.loads(Path(__file__).with_name('scenario.json').read_text())
            return build_service(scenario)
    ''')
    final_scope = rng.choice(('tenant', 'request')) if tier in ('hard', 'expert') else 'root'
    if tier in ('hard', 'expert'):
        selected = 1 if final_scope == 'tenant' else 2
    _event_dispatch(rng, tier, files, modules, symbol, 'from container import lookup_provider\n',
                    'lookup_provider(context)', {'scope': 'preview'}, {'scope': final_scope})
    return _pack(rng, tier, 'scoped service binding', files, modules[selected], symbol)


def _routes(rng, tier):
    files, names, modules, symbol = _handlers(rng)
    endpoint = '/%s/%d' % (rng.choice(('orders', 'exports', 'reports')), rng.randrange(100, 999))
    verb = rng.choice(('GET', 'POST', 'PUT'))
    flags = {'new_pipeline': bool(rng.randrange(2)), 'preview': False}
    rows = {endpoint: {'GET': {'handler': names[0]}, 'POST': {'handler': names[0]}, 'PUT': {'handler': names[0]}},
            '/canonical': {verb: {'handler': names[1]}},
            '/accelerated': {verb: {'handler': names[2]}},
            '/preview': {verb: {'handler': names[3]}},
            '/unused': {verb: {'alias': '/preview'}}}
    selected = 0
    if tier != 'easy':
        rows[endpoint][verb] = {'alias': '/canonical'}
        selected = 1
    if tier in ('hard', 'expert'):
        rows['/canonical'][verb] = {'feature': 'new_pipeline', 'on': '/accelerated', 'off': '/legacy'}
        rows['/legacy'] = {verb: {'handler': names[1]}}
        selected = 2 if flags['new_pipeline'] else 1
    if tier == 'expert':
        # The requested method differs from an attractive preview fallback.
        rows[endpoint]['*'] = {'alias': '/preview'}
        rows['/accelerated'][verb] = {'alias': '/final'}
        rows['/final'] = {'*': {'handler': names[3]}, verb: {'handler': names[2]}}
        rows['/legacy'][verb] = {'alias': '/compat'}
        rows['/compat'] = {'*': {'handler': names[3]}, verb: {'handler': names[1]}}
        rows['/unused'][verb] = {'alias': '/unused'}
        rows['/accelerated'][verb] = {'alias': '/hop_0'}
        for index in range(7):
            following = '/hop_%d' % (index + 1) if index < 6 else '/final'
            rows['/hop_%d' % index] = {'*': {'alias': '/preview'}, verb: {
                'feature': 'preview', 'on': '/preview', 'off': following}}
    files['routes.json'] = _json(rows)
    files['scenario.json'] = _json({'path': endpoint, 'method': verb, 'features': flags})
    files['views.py'] = '\n'.join('from %s import %s as view_%d' % (module, symbol, i)
                                   for i, module in enumerate(modules)) + '\n\nHANDLERS = {'
    files['views.py'] += ', '.join('%r: view_%d' % (name, i) for i, name in enumerate(names)) + '}\n'
    lookup = "rows[path].get(method, rows[path].get('*'))" if tier == 'expert' else "rows[path][method]"
    files['routing.py'] = (
        "import json\nfrom pathlib import Path\nfrom views import HANDLERS\n\n"
        "def dispatch(scenario):\n"
        "    rows = json.loads(Path(__file__).with_name('routes.json').read_text())\n"
        "    path, method = scenario['path'], scenario['method']\n    visited = set()\n"
        "    while True:\n        if path in visited: raise ValueError('route cycle')\n"
        "        visited.add(path)\n        route = %s\n"
        "        if 'alias' in route:\n            path = route['alias']\n"
        "        elif 'feature' in route:\n"
        "            path = route['on'] if scenario['features'].get(route['feature'], False) else route['off']\n"
        "        else:\n            return HANDLERS[route['handler']]\n" % lookup)
    files['entry.py'] = _source('''
        import json
        from pathlib import Path
        from routing import dispatch as route_request

        def resolve():
            scenario = json.loads(Path(__file__).with_name('scenario.json').read_text())
            return route_request(scenario)
    ''')
    final_features = dict(flags, new_pipeline=not flags['new_pipeline'])
    if tier in ('hard', 'expert'):
        selected = 2 if final_features['new_pipeline'] else 1
    _event_dispatch(rng, tier, files, modules, symbol, 'from routing import dispatch\n', 'dispatch(context)',
                    {'path': '/preview'}, {'features': final_features})
    return _pack(rng, tier, 'HTTP route', files, modules[selected], symbol)


def make(rng, template_index, tier):
    if tier not in TIERS:
        raise ValueError('unknown repo-qa tier')
    return (_config, _plugins, _injection, _routes)[template_index](rng, tier)
