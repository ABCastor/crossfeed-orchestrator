"""Caller-declared material classification. Unknown material is private.

Classification covers the entire prompt, attachments, context and tool inputs.
No heuristic can establish that arbitrary repository contents are public.
"""
import os


def classification(value=None):
    value = value if value is not None else os.environ.get('FLEET_DATA_CLASS', 'private')
    if value not in {'public', 'private'}:
        raise ValueError('data class must be public or private')
    return value


def refusal(lane, data_class=None):
    # BR-DATA-001
    material = classification(data_class)
    promo = lane.get('free_promo') or str(lane.get('selector', '')).endswith('-free') or str(lane.get('model_key', '')).endswith('-free')
    policy = lane.get('data_policy', 'public-only' if promo else 'private-ok')
    if policy not in {'public-only', 'private-ok'}:
        return 'invalid lane data_policy'
    if material == 'private' and policy != 'private-ok':
        return 'public-only lane refuses private material'
    return None
