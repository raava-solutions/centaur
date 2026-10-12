import { assertSlackOk, callSlackApi } from '@chat-adapter/slack/api'
import {
  APPROVAL_ACTION_PREFIX,
  decideApprovalRequest,
  getApprovalRequest,
  listApprovalsAwaitingSlack,
  recordApprovalSlackDelivery,
  withSlackApiTimeout
} from './session-api'
import type {
  JsonObject,
  SlackbotV2ApprovalRequest,
  SlackbotV2BlockActionPayload,
  SlackbotV2Options
} from './types'
import { errorMessage, stringValue, traceLog, traceWarn } from './utils'

const DEFAULT_APPROVAL_POLL_INTERVAL_MS = 3_000
const APPROVAL_ID_PATTERN = /^apr_[0-9a-f]{32}$/

type SlackThreadDestination = {
  channelId: string
  threadTs: string
}

/**
 * Background loop posting pending approval requests into their Slack threads
 * as Block Kit messages with Approve/Deny buttons. Delivery is recorded at
 * api-rs, so restarts resume with whatever is still undelivered; a request
 * whose TTL lapses before delivery simply drops out of the poll result and
 * resolves `expired` for the waiting tool call.
 *
 * Runs only when at least one approver is configured: without an approver
 * allowlist nothing could ever decide, so posting buttons would be noise.
 */
export function startApprovalDeliveryLoop(options: SlackbotV2Options): void {
  if (!options.approverSlackUserIds?.length) return
  const intervalMs = options.approvalPollIntervalMs ?? DEFAULT_APPROVAL_POLL_INTERVAL_MS
  // Threads whose key does not name a Slack destination are logged once per
  // process instead of on every poll tick.
  const undeliverableLogged = new Set<string>()
  let inFlight = false
  const tick = async (): Promise<void> => {
    if (inFlight) return
    inFlight = true
    try {
      const approvals = await listApprovalsAwaitingSlack(options)
      for (const approval of approvals) {
        try {
          const delivered = await deliverApprovalRequest(options, approval)
          if (!delivered && !undeliverableLogged.has(approval.id)) {
            undeliverableLogged.add(approval.id)
            traceLog(options, 'slackbotv2_approval_delivery_skipped', undefined, {
              approval_id: approval.id,
              reason: 'thread key is not a Slack thread',
              thread_key: approval.thread_key
            })
          }
        } catch (error) {
          traceWarn(options, 'slackbotv2_approval_delivery_failed', undefined, {
            approval_id: approval.id,
            error: errorMessage(error),
            thread_key: approval.thread_key
          })
        }
      }
    } catch (error) {
      traceWarn(options, 'slackbotv2_approval_poll_failed', undefined, {
        error: errorMessage(error)
      })
    } finally {
      inFlight = false
    }
  }
  const timer = setInterval(() => {
    void tick()
  }, intervalMs)
  timer.unref?.()
  void tick()
}

/** Post one approval request into its Slack thread. Returns false when the
 * thread key does not name a Slack destination. */
async function deliverApprovalRequest(
  options: SlackbotV2Options,
  approval: SlackbotV2ApprovalRequest
): Promise<boolean> {
  const thread = slackThreadFromThreadKey(approval.thread_key)
  if (!thread) return false
  const response = await withSlackApiTimeout(options, 'post approval request', () =>
    callSlackApi(
      'chat.postMessage',
      {
        channel: thread.channelId,
        thread_ts: thread.threadTs,
        text: approvalFallbackText(approval),
        blocks: approvalRequestBlocks(approval)
      },
      {
        apiUrl: options.slackApiUrl,
        fetch: options.fetch as typeof globalThis.fetch | undefined,
        token: options.botToken
      }
    )
  )
  assertSlackOk('chat.postMessage', response)
  const messageTs = stringValue(response.ts)
  if (!messageTs) throw new Error('chat.postMessage returned no message ts')
  await recordApprovalSlackDelivery(options, approval.id, thread.channelId, messageTs)
  traceLog(options, 'slackbotv2_approval_delivered', undefined, {
    approval_id: approval.id,
    channel_id: thread.channelId,
    message_ts: messageTs,
    thread_key: approval.thread_key
  })
  return true
}

/**
 * Handle a signed Approve/Deny click. Only configured approver user IDs may
 * decide; anyone else gets an ephemeral refusal and the request stays pending.
 * Decisions are one-shot at api-rs: a second click (or a click after expiry)
 * re-reads the durable state and renders that instead of inventing one.
 */
export async function dispatchApprovalAction(
  options: SlackbotV2Options,
  payload: SlackbotV2BlockActionPayload
): Promise<void> {
  const decision = payload.action_id.slice(APPROVAL_ACTION_PREFIX.length)
  const approvalId = payload.value ?? ''
  if ((decision !== 'approve' && decision !== 'deny') || !APPROVAL_ID_PATTERN.test(approvalId)) {
    traceWarn(options, 'slackbotv2_approval_action_invalid', undefined, {
      action_id: payload.action_id,
      user_id: payload.user_id
    })
    return
  }
  const fields = {
    approval_id: approvalId,
    channel_id: payload.channel_id,
    decision,
    message_ts: payload.message_ts,
    user_id: payload.user_id
  }

  if (!options.approverSlackUserIds?.includes(payload.user_id)) {
    traceWarn(options, 'slackbotv2_approval_action_unauthorized', undefined, fields)
    await postApprovalEphemeral(
      options,
      payload,
      'Not authorized: only designated approvers can decide this request.'
    )
    return
  }

  const decided = await decideApprovalRequest(
    options,
    approvalId,
    decision === 'approve' ? 'approved' : 'denied',
    payload.user_id
  ).catch(async (error: unknown) => {
    traceWarn(options, 'slackbotv2_approval_decide_failed', undefined, {
      ...fields,
      error: errorMessage(error)
    })
    await postApprovalEphemeral(
      options,
      payload,
      'This approval could not be recorded. Please try again.'
    )
    return null
  })
  if (decided === null) return
  // A conflict means the durable record moved on without us (double click,
  // expiry race): render the true current state, never the clicked one.
  const approval = decided ?? (await getApprovalRequest(options, approvalId))
  traceLog(options, 'slackbotv2_approval_action_processed', undefined, {
    ...fields,
    outcome: decided ? approval.status : 'already_decided'
  })
  if (payload.channel_id && payload.message_ts) {
    await withSlackApiTimeout(options, 'update approval message', () =>
      callSlackApi(
        'chat.update',
        {
          channel: payload.channel_id,
          ts: payload.message_ts,
          text: approvalFallbackText(approval),
          blocks: approvalDecisionBlocks(approval)
        },
        {
          apiUrl: options.slackApiUrl,
          fetch: options.fetch as typeof globalThis.fetch | undefined,
          token: options.botToken
        }
      )
    )
      .then(response => assertSlackOk('chat.update', response))
      .catch((error: unknown) => {
        // The decision is durable; a failed re-render must not lose it.
        traceWarn(options, 'slackbotv2_approval_message_update_failed', undefined, {
          ...fields,
          error: errorMessage(error)
        })
      })
  }
}

async function postApprovalEphemeral(
  options: SlackbotV2Options,
  payload: SlackbotV2BlockActionPayload,
  text: string
): Promise<void> {
  if (!payload.channel_id) return
  try {
    await withSlackApiTimeout(options, 'post approval feedback', () =>
      callSlackApi(
        'chat.postEphemeral',
        {
          channel: payload.channel_id,
          user: payload.user_id,
          ...(payload.thread_ts ? { thread_ts: payload.thread_ts } : {}),
          text
        },
        {
          apiUrl: options.slackApiUrl,
          fetch: options.fetch as typeof globalThis.fetch | undefined,
          token: options.botToken
        }
      )
    )
  } catch (error) {
    traceWarn(options, 'slackbotv2_approval_feedback_failed', undefined, {
      approval_id: payload.value,
      error: errorMessage(error),
      user_id: payload.user_id
    })
  }
}

function slackThreadFromThreadKey(threadKey: string): SlackThreadDestination | undefined {
  const parts = threadKey.split(':')
  if (parts[0] !== 'slack') return undefined
  if (parts.length === 3 && parts[1] && parts[2]) {
    return { channelId: parts[1], threadTs: parts[2] }
  }
  if (parts.length === 4 && parts[1] && parts[2] && parts[3]) {
    return { channelId: parts[2], threadTs: parts[3] }
  }
  return undefined
}

function approvalFallbackText(approval: SlackbotV2ApprovalRequest): string {
  const base = `Approval ${approval.status}: ${approval.action_summary}`
  return approval.approver ? `${base} (by <@${approval.approver}>)` : base
}

function approvalContextElements(approval: SlackbotV2ApprovalRequest): JsonObject[] {
  const elements: JsonObject[] = [
    { type: 'mrkdwn', text: `Request \`${approval.id}\`` },
    { type: 'mrkdwn', text: `Thread \`${approval.thread_key}\`` },
    { type: 'mrkdwn', text: `Action hash \`${truncateHash(approval.action_hash)}\`` },
    { type: 'mrkdwn', text: `Expires <!date^${expiryEpoch(approval)}^{date_short} {time}|${approval.expires_at}>` }
  ]
  if (approval.requester) {
    elements.splice(1, 0, { type: 'mrkdwn', text: `Requested by ${approval.requester}` })
  }
  return elements
}

function expiryEpoch(approval: SlackbotV2ApprovalRequest): number {
  const parsed = Date.parse(approval.expires_at)
  return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : 0
}

function truncateHash(hash: string): string {
  return hash.length > 16 ? `${hash.slice(0, 16)}…` : hash
}

export function approvalRequestBlocks(approval: SlackbotV2ApprovalRequest): JsonObject[] {
  return [
    {
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: `:lock: *Approval requested*\n${approval.action_summary}`
      }
    },
    { type: 'context', elements: approvalContextElements(approval) },
    {
      type: 'actions',
      elements: [
        {
          type: 'button',
          text: { type: 'plain_text', text: 'Approve' },
          style: 'primary',
          action_id: `${APPROVAL_ACTION_PREFIX}approve`,
          value: approval.id
        },
        {
          type: 'button',
          text: { type: 'plain_text', text: 'Deny' },
          style: 'danger',
          action_id: `${APPROVAL_ACTION_PREFIX}deny`,
          value: approval.id
        }
      ]
    }
  ]
}

export function approvalDecisionBlocks(approval: SlackbotV2ApprovalRequest): JsonObject[] {
  const statusLine = approvalStatusLine(approval)
  return [
    {
      type: 'section',
      text: { type: 'mrkdwn', text: `${statusLine}\n${approval.action_summary}` }
    },
    { type: 'context', elements: approvalContextElements(approval) }
  ]
}

function approvalStatusLine(approval: SlackbotV2ApprovalRequest): string {
  if (approval.status === 'approved' || approval.status === 'denied') {
    const icon = approval.status === 'approved' ? ':white_check_mark:' : ':no_entry_sign:'
    const who = approval.approver ? `<@${approval.approver}>` : 'unknown'
    const when = approval.decided_at ?? ''
    return `${icon} *${capitalize(approval.status)}* by ${who}${when ? ` at ${when}` : ''}`
  }
  if (approval.status === 'expired') {
    return ':hourglass: *Expired* — no decision before the deadline; the action was not approved'
  }
  return ':lock: *Approval requested*'
}

function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1)
}
