"""Caller-declared material classification. Unknown material is private.

Classification covers the entire prompt, attachments, context and tool inputs.
No heuristic can establish that arbitrary repository contents are public.
"code" is private project source code and its docs, with no notes, memory,
personal or client data, or secrets.
"""
import os

# Material classes, least to most sensitive. A lane policy accepts everything
# up to its ceiling: public-only -> public, code-ok -> code, private-ok -> private.
MATERIAL_ORDER = ('public', 'code', 'private')
POLICY_CEILING = {'public-only': 'public', 'code-ok': 'code', 'private-ok': 'private'}

def classification(value=None):
    value = value if value is not None else os.environ.get('FLEET_DATA_CLASS', 'private')
    if value not in MATERIAL_ORDER:
        raise ValueError('data class must be public, code or private')
    return value


def refusal(lane, data_class=None):
    # BR-DATA-001
    material = classification(data_class)
    promo = lane.get('free_promo') or str(lane.get('selector', '')).endswith('-free') or str(lane.get('model_key', '')).endswith('-free')
    policy = lane.get('data_policy', 'public-only' if promo else 'private-ok')
    if policy not in POLICY_CEILING:
        return 'invalid lane data_policy'
    if MATERIAL_ORDER.index(material) > MATERIAL_ORDER.index(POLICY_CEILING[policy]):
        return f'{policy} lane refuses {material} material'
    return None
