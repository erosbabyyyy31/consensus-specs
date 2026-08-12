# Minimal reference test for Electra fork upgrade.
# Adapt imports and fixtures according to the repo's pyspec test harness.

import pytest

# TODO: replace these imports with the repo's spec helpers
# from eth2spec.electra.fork import upgrade_to_electra
# from eth2spec.utils import get_activation_exit_churn_limit, get_consolidation_churn_limit

def test_upgrade_moves_inactive_validators_to_pending_deposits(sample_state):
    """
    Smoke test: ensure that inactive validators are converted to pending deposits
    and churn consumption counters are initialized.
    """
    pre = sample_state
    # Precondition: at least one inactive validator exists
    assert any(v.activation_epoch == FAR_FUTURE_EPOCH for v in pre.validators)

    post = upgrade_to_electra(pre)

    # Fork version updated
    assert post.fork.current_version == ELECTRA_FORK_VERSION

    # Inactive validators handled as pending deposits and balances zeroed
    pending_pubkeys = {pd.pubkey for pd in post.pending_deposits}
    for idx, v in enumerate(pre.validators):
        if v.activation_epoch == FAR_FUTURE_EPOCH:
            # the original validator balance has been zeroed in post.balances
            assert post.balances[idx] == 0
            # the validator effective balance was zeroed
            assert post.validators[idx].effective_balance == 0
            # the pubkey from pre should appear in pending_deposits
            assert v.pubkey in pending_pubkeys

    # Churn values initialized
    assert post.exit_balance_to_consume == get_activation_exit_churn_limit(post)
    assert post.consolidation_balance_to_consume == get_consolidation_churn_limit(post)
