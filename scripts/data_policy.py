"""Caller-declared material classification. Unknown material is private.

Classification covers the entire prompt, attachments, context and tool inputs.
No heuristic can establish that arbitrary repository contents are public.
"code" is private project source code and its docs, with no notes, memory,
personal or client data, or secrets.
"""
import hashlib
import os
import re

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


# BR-CASH-003
def paid_refusal(route, data_class=None, prompt=None):
    """A caller label cannot authorize disclosure. The owner pins exact outgoing bytes."""
    try:
        material = classification(data_class)
    except ValueError:
        return "unrecognized material class; refusing paid route"
    classes = route.get("material_classes", ["public"])
    if classes != ["public"]:
        return "this transport supports only public paid material"
    if material not in classes:
        return f"public-only paid route refuses {material} material"
    hashes = route.get("approved_prompt_sha256")
    if not isinstance(hashes, list) or not hashes or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
        return "paid route has no valid owner-approved public prompt hashes"
    if prompt is not None and hashlib.sha256(prompt.encode("utf-8")).hexdigest() not in hashes:
        return "exact paid prompt is not owner-approved"
    return None
