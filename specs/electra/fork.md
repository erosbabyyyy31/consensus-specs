# Electra -- Fork Logic

<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=2 -->

- [Introduction](#introduction)
- [Configuration](#configuration)
- [Fork to Electra](#fork-to-electra)
  - [Fork trigger](#fork-trigger)
  - [Upgrading the state](#upgrading-the-state)
- [Changelog](#changelog)
- [Testing](#testing)
- [References](#references)

<!-- mdformat-toc end -->

## Introduction

This document describes the process of the Electra upgrade and the irregular state transition used to upgrade an existing BeaconState to Electra.

Electra introduces new state fields and new queues (deposit requests, pending partial withdrawals/consolidations) and modifies activation/exit/consolidation churn handling to support compounding credentials and other features described in the related EIPs.

## Configuration

Warning: this configuration is not definitive.

| Name                   | Value                                         |
| ---------------------- | --------------------------------------------- |
| `ELECTRA_FORK_VERSION` | `Version('0x05000000')`                       |
| `ELECTRA_FORK_EPOCH`   | `Epoch(364032)` (May 7, 2025, 10:05:11am UTC) |

## Fork to Electra

### Fork trigger

The fork is triggered at epoch `ELECTRA_FORK_EPOCH`.

*Note*: For the pure Electra networks, the `upgrade_to_electra` function is applied to transition the genesis state to this fork.

### Upgrading the state

If `state.slot % SLOTS_PER_EPOCH == 0` and
`compute_epoch_at_slot(state.slot) == ELECTRA_FORK_EPOCH`, an irregular state
change is made to upgrade to Electra.

The upgrade performs these main actions:
- Set the fork current_version to `ELECTRA_FORK_VERSION`.
- Add Electra-specific fields to the state (deposit request queues, pending withdrawal/consolidation queues, churn consumption counters, earliest epochs).
- Convert validators that are not yet active (activation_epoch == FAR_FUTURE_EPOCH) into pending deposits (so they enter the standard deposit/activation flow under Electra).
- Ensure validators using compounding withdrawal credentials get queued for activation churn processing.

Below is a cleaned, documented version of the upgrade function.

```python
def upgrade_to_electra(pre: deneb.BeaconState) -> deneb.BeaconState:
    """
    Convert an existing BeaconState 'pre' into an Electra-ready BeaconState 'post'.

    Notes:
    - Uses deneb-qualified helpers for clarity.
    - Validators not yet active become PendingDeposit entries and have balances/effective balances zeroed.
    - GENESIS_SLOT and bls.G2_POINT_AT_INFINITY are used to mark these deposits as
      originating from the fork conversion (distinguish from real external deposits).
    """
    # current epoch at time of upgrade
    epoch = deneb.get_current_epoch(pre)

    # Determine earliest_exit_epoch for consolidation/exit churn initialization.
    earliest_exit_epoch = compute_activation_exit_epoch(deneb.get_current_epoch(pre))
    for validator in pre.validators:
        if validator.exit_epoch != FAR_FUTURE_EPOCH:
            # keep the epoch at least as late as any existing exit_epoch already requested
            earliest_exit_epoch = max(earliest_exit_epoch, validator.exit_epoch)
    # Move to the epoch after the last exit to ensure churn windows do not overlap incorrectly
    earliest_exit_epoch += Epoch(1)

    # Create new BeaconState with Electra fork metadata and new fields added
    post = deneb.BeaconState(
        # keep existing genesis/chain identifiers
        genesis_time=pre.genesis_time,
        genesis_validators_root=pre.genesis_validators_root,
        slot=pre.slot,
        # update fork information
        fork=Fork(
            previous_version=pre.fork.current_version,
            # [Modified in Electra]
            current_version=ELECTRA_FORK_VERSION,
            epoch=epoch,
        ),
        # copy remaining canonical state fields unchanged
        latest_block_header=pre.latest_block_header,
        block_roots=pre.block_roots,
        state_roots=pre.state_roots,
        historical_roots=pre.historical_roots,
        eth1_data=pre.eth1_data,
        eth1_data_votes=pre.eth1_data_votes,
        eth1_deposit_index=pre.eth1_deposit_index,
        validators=pre.validators,
        balances=pre.balances,
        randao_mixes=pre.randao_mixes,
        slashings=pre.slashings,
        previous_epoch_participation=pre.previous_epoch_participation,
        current_epoch_participation=pre.current_epoch_participation,
        justification_bits=pre.justification_bits,
        previous_justified_checkpoint=pre.previous_justified_checkpoint,
        current_justified_checkpoint=pre.current_justified_checkpoint,
        finalized_checkpoint=pre.finalized_checkpoint,
        inactivity_scores=pre.inactivity_scores,
        current_sync_committee=pre.current_sync_committee,
        next_sync_committee=pre.next_sync_committee,
        latest_execution_payload_header=pre.latest_execution_payload_header,
        next_withdrawal_index=pre.next_withdrawal_index,
        next_withdrawal_validator_index=pre.next_withdrawal_validator_index,
        historical_summaries=pre.historical_summaries,

        # -------------------------
        # New in Electra (EIP-6110, EIP-7251)
        # Brief inline explanations:
        # - deposit_requests_start_index: index into a deposit request queue (UNSET indicates empty/not-started)
        # - deposit_balance_to_consume / exit_balance_to_consume / consolidation_balance_to_consume:
        #   churn consumption counters used as part of limited processing per epoch
        # - earliest_exit_epoch / earliest_consolidation_epoch: earliest epochs allowed for exits/consolidations to be processed
        # - pending_deposits: pending deposits created by the fork conversion (converted inactive validators)
        # - pending_partial_withdrawals / pending_consolidations: queues for partial withdrawals and consolidations
        deposit_requests_start_index=UNSET_DEPOSIT_REQUESTS_START_INDEX,
        deposit_balance_to_consume=0,
        exit_balance_to_consume=0,
        earliest_exit_epoch=earliest_exit_epoch,
        consolidation_balance_to_consume=0,
        earliest_consolidation_epoch=compute_activation_exit_epoch(deneb.get_current_epoch(pre)),
        pending_deposits=PendingDeposits(),
        pending_partial_withdrawals=PendingPartialWithdrawals(),
        pending_consolidations=PendingConsolidations(),
    )

    # initialize consumption values using the helper functions so post is consistent
    post.exit_balance_to_consume = get_activation_exit_churn_limit(post)
    post.consolidation_balance_to_consume = get_consolidation_churn_limit(post)

    # Convert validators that are not yet active into PendingDeposit queue entries.
    # This ensures inactive validators still go through the standard deposit activation path
    # under Electra semantics rather than remaining as inactive validators with pre-upgrade balances.
    pre_activation = sorted(
        [
            index
            for index, validator in enumerate(post.validators)
            if validator.activation_epoch == FAR_FUTURE_EPOCH
        ],
        key=lambda index: (post.validators[index].activation_eligibility_epoch, index),
    )

    for index in pre_activation:
        balance = post.balances[index]
        # zero the validator's on-chain balance and effective balance (they become a pending deposit)
        post.balances[index] = 0
        validator = post.validators[index]
        validator.effective_balance = 0
        validator.activation_eligibility_epoch = FAR_FUTURE_EPOCH

        # Use bls.G2_POINT_AT_INFINITY as a signature placeholder and GENESIS_SLOT to indicate
        # this pending deposit originates from the fork upgrade (not an on-chain deposit).
        post.pending_deposits.append(
            PendingDeposit(
                pubkey=validator.pubkey,
                withdrawal_credentials=validator.withdrawal_credentials,
                amount=balance,
                signature=bls.G2_POINT_AT_INFINITY,
                slot=GENESIS_SLOT,
            )
        )

    # Ensure early adopters of compounding withdrawal credentials go through activation churn
    for index, validator in enumerate(post.validators):
        if has_compounding_withdrawal_credential(validator):
            queue_excess_active_balance(post, ValidatorIndex(index))

    return post
```
