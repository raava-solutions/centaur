use std::env;
use std::process::Command as ProcessCommand;

use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use codex_app_server_protocol::UserInput;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::{
    HarnessKind, HarnessServer, NormalizedContent, NormalizedEvent, NormalizedTokenUsage,
    NormalizedToolResult, Result, ThreadState, command_from_override, stable_id,
};

/// Pi harness: drives the `pi` CLI's long-lived RPC mode (`pi --mode rpc`).
/// One child process per thread; each turn writes one JSONL `prompt` command
/// to stdin and consumes session events from stdout until `agent_settled`
/// (Pi's guarantee that no automatic work — retries, compaction recovery,
/// queued steering — remains for the run). Session continuity comes from
/// `--session-id`: Pi creates the session when missing and reloads it after a
/// child restart, so an interrupt (which kills the child) does not lose
/// conversational memory as long as `~/.pi` persists.
#[derive(Debug, Default)]
pub struct PiHarness;

impl HarnessServer for PiHarness {
    type Event = PiEvent;
    type EventNormalizer = PiEventNormalizer;

    fn kind(&self) -> HarnessKind {
        HarnessKind::Pi
    }

    fn cli_version(&self) -> &'static str {
        "pi"
    }

    /// Empty when no explicit override exists: the model is owned by the
    /// in-image pi settings (`~/.pi/agent/settings.json` `defaultModel`,
    /// rendered by the sandbox entrypoint from the same env var). An empty
    /// model means `command_for_turn` omits `--model` so the CLI falls through
    /// to settings.json.
    fn default_model(&self) -> String {
        env::var("PI_DEFAULT_MODEL").unwrap_or_default()
    }

    fn default_model_provider(&self) -> &'static str {
        "pi"
    }

    fn command_for_turn(&self, state: &ThreadState) -> ProcessCommand {
        if let Some(command) = command_from_override("CENTAUR_PI_APP_BRIDGE_COMMAND") {
            return command;
        }

        let bin = env::var("PI_BIN").unwrap_or_else(|_| "pi".to_string());
        let mut command = ProcessCommand::new(bin);
        command.args(["--mode", "rpc"]);
        if !state.model.is_empty() {
            // pi's model patterns accept both bare ids and `provider/id`.
            command.args(["--model", &state.model]);
        }
        // `--session-id` pins the session file across child respawns (interrupt
        // kills the process; the next turn respawns and reloads the session)
        // and creates it when missing, so it serves both first and later turns.
        let session_id = state.harness_session_id.as_deref().unwrap_or(&state.id);
        command.args(["--session-id", session_id]);
        command
    }

    fn stdin_for_turn(&self, input: &[UserInput]) -> Result<Vec<u8>> {
        pi_command("prompt", input)
    }

    fn stdin_for_steer(&self, input: &[UserInput]) -> Result<Vec<u8>> {
        pi_command("steer", input)
    }

    fn parse_stdout_line(&self, line: &str) -> Result<Self::Event> {
        Ok(serde_json::from_str(line)?)
    }

    fn normalize_events(
        &self,
        normalizer: &mut Self::EventNormalizer,
        event: Self::Event,
    ) -> Result<Vec<NormalizedEvent>> {
        Ok(normalizer.normalize(event))
    }
}

/// Build one RPC command line from turn input: text parts join into `message`;
/// local images are read and inlined as base64 `images` (Pi's prompt/steer
/// command shape). Remote image URLs and skill/mention references degrade to
/// text, matching how the other harnesses describe non-text input.
fn pi_command(kind: &str, input: &[UserInput]) -> Result<Vec<u8>> {
    let mut texts = Vec::new();
    let mut images = Vec::new();
    for item in input {
        match item {
            UserInput::Text { text, .. } => texts.push(text.clone()),
            UserInput::LocalImage { path, .. } => {
                let bytes = std::fs::read(path)?;
                let mime_type = mime_type_for_image(path);
                texts.push(format!("[Attached image: {}]", path.display()));
                images.push(json!({
                    "type": "image",
                    "data": BASE64_STANDARD.encode(bytes),
                    "mimeType": mime_type,
                }));
            }
            UserInput::Image { url, .. } => texts.push(format!("[image: {url}]")),
            UserInput::Skill { name, path } => {
                texts.push(format!("[skill: {name} at {}]", path.display()));
            }
            UserInput::Mention { name, path } => {
                texts.push(format!("[mention: {name} at {path}]"));
            }
        }
    }
    let mut command = json!({
        "type": kind,
        "message": texts.join("\n"),
    });
    if !images.is_empty() {
        command["images"] = Value::Array(images);
    }
    let mut bytes = serde_json::to_vec(&command)?;
    bytes.push(b'\n');
    Ok(bytes)
}

fn mime_type_for_image(path: &std::path::Path) -> &'static str {
    match path
        .extension()
        .and_then(|extension| extension.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        _ => "image/png",
    }
}

/// One stdout record from `pi --mode rpc`. Only the fields the bridge consumes
/// are typed; everything else passes through serde's default ignoring.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum PiEvent {
    AgentStart,
    AgentEnd {
        #[serde(default)]
        will_retry: bool,
    },
    AgentSettled,
    MessageStart {
        message: PiMessage,
    },
    MessageUpdate {
        #[serde(rename = "assistantMessageEvent")]
        assistant_message_event: PiAssistantMessageEvent,
    },
    MessageEnd {
        message: PiMessage,
    },
    ToolExecutionEnd {
        #[serde(rename = "toolCallId")]
        tool_call_id: Option<String>,
        result: Option<Value>,
        #[serde(rename = "isError", default)]
        is_error: bool,
    },
    /// RPC command response. A rejected prompt (`success: false`) means no run
    /// started for the turn, so it must surface as a turn error rather than
    /// leave the bridge waiting for an `agent_settled` that never comes.
    #[serde(rename = "response")]
    Response {
        command: Option<String>,
        #[serde(default)]
        success: bool,
        error: Option<String>,
    },
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PiMessage {
    pub role: Option<String>,
    #[serde(rename = "stopReason")]
    pub stop_reason: Option<String>,
    #[serde(rename = "errorMessage")]
    pub error_message: Option<String>,
    pub model: Option<String>,
    pub usage: Option<PiUsage>,
    // System/user messages carry a plain string; only assistant content blocks
    // are structured.
    #[serde(default, deserialize_with = "deserialize_content_blocks")]
    pub content: Vec<PiContentBlock>,
}

fn deserialize_content_blocks<'de, D>(
    deserializer: D,
) -> std::result::Result<Vec<PiContentBlock>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Content {
        Blocks(Vec<PiContentBlock>),
        // System/user messages carry a plain string (or anything else); only
        // assistant content blocks are structured.
        Other(serde::de::IgnoredAny),
    }
    Ok(match Content::deserialize(deserializer)? {
        Content::Blocks(blocks) => blocks,
        Content::Other(_) => Vec::new(),
    })
}

#[derive(Debug, Clone, Deserialize)]
pub struct PiUsage {
    pub input: Option<i64>,
    pub output: Option<i64>,
    #[serde(rename = "cacheRead")]
    pub cache_read: Option<i64>,
    #[serde(rename = "cacheWrite")]
    pub cache_write: Option<i64>,
    pub reasoning: Option<i64>,
    #[serde(rename = "totalTokens")]
    pub total_tokens: Option<i64>,
}

impl PiUsage {
    fn into_normalized(self, model: Option<String>) -> Option<NormalizedTokenUsage> {
        let usage = NormalizedTokenUsage {
            model,
            input_tokens: self.input,
            output_tokens: self.output,
            cache_creation_input_tokens: self.cache_write,
            cache_read_input_tokens: self.cache_read,
            reasoning_output_tokens: self.reasoning,
            total_tokens: self.total_tokens,
        };
        usage.has_counts().then_some(usage)
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type")]
pub enum PiContentBlock {
    #[serde(rename = "text")]
    Text { text: String },
    #[serde(rename = "thinking")]
    Thinking {
        thinking: Option<String>,
        text: Option<String>,
    },
    #[serde(rename = "toolCall")]
    ToolCall {
        id: Option<String>,
        name: Option<String>,
        arguments: Option<Value>,
    },
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type")]
pub enum PiAssistantMessageEvent {
    #[serde(rename = "text_delta")]
    TextDelta { delta: String },
    #[serde(rename = "thinking_delta")]
    ThinkingDelta {
        #[serde(rename = "contentIndex")]
        content_index: usize,
        delta: String,
    },
    #[serde(other)]
    Unknown,
}

/// Per-turn pi event normalizer. Pi messages carry no ids, so assistant item
/// ids are synthesized from a per-turn message counter. The turn's terminal
/// event is `agent_settled`; its `error` is the last assistant message's
/// `errorMessage` when that message stopped with `error`/`aborted`, or the
/// final auto-retry failure.
#[derive(Debug, Default)]
pub struct PiEventNormalizer {
    message_index: usize,
    last_error: Option<String>,
}

impl PiEventNormalizer {
    fn normalize(&mut self, event: PiEvent) -> Vec<NormalizedEvent> {
        match event {
            PiEvent::AgentStart | PiEvent::Unknown => vec![NormalizedEvent::Ignored],
            PiEvent::AgentEnd { .. } => vec![NormalizedEvent::Ignored],
            PiEvent::AgentSettled => vec![NormalizedEvent::Result {
                error: self.last_error.take(),
            }],
            PiEvent::Response {
                command,
                success,
                error,
            } => {
                if success {
                    return vec![NormalizedEvent::Ignored];
                }
                match command.as_deref() {
                    Some("prompt") => vec![NormalizedEvent::Error {
                        message: error.unwrap_or_else(|| "pi rejected the prompt".to_string()),
                    }],
                    // A failed steer/abort does not end the turn.
                    _ => vec![NormalizedEvent::Ignored],
                }
            }
            PiEvent::MessageStart { message } => {
                if message.role.as_deref() == Some("assistant") {
                    self.message_index += 1;
                }
                vec![NormalizedEvent::Ignored]
            }
            PiEvent::MessageUpdate {
                assistant_message_event,
            } => {
                let item_id = self.current_item_id();
                let event = match assistant_message_event {
                    PiAssistantMessageEvent::TextDelta { delta } => {
                        NormalizedEvent::AgentTextDelta { item_id, delta }
                    }
                    PiAssistantMessageEvent::ThinkingDelta {
                        content_index,
                        delta,
                    } => NormalizedEvent::ReasoningTextDelta {
                        item_id: reasoning_item_id(&item_id, content_index),
                        delta,
                    },
                    PiAssistantMessageEvent::Unknown => NormalizedEvent::Ignored,
                };
                vec![event]
            }
            PiEvent::MessageEnd { message } => self.normalize_message_end(message),
            PiEvent::ToolExecutionEnd {
                tool_call_id,
                result,
                is_error,
            } => {
                let (content, exit_code) = tool_result_content(result.as_ref());
                vec![NormalizedEvent::ToolResults(vec![NormalizedToolResult {
                    tool_use_id: tool_call_id.unwrap_or_else(|| "tool".to_string()),
                    content,
                    is_error,
                    exit_code,
                }])]
            }
        }
    }

    fn normalize_message_end(&mut self, message: PiMessage) -> Vec<NormalizedEvent> {
        if message.role.as_deref() != Some("assistant") {
            return vec![NormalizedEvent::Ignored];
        }
        let item_id = self.current_item_id();
        let stop_reason = message
            .stop_reason
            .as_deref()
            .map(normalize_stop_reason)
            .map(str::to_string);
        if matches!(message.stop_reason.as_deref(), Some("error" | "aborted")) {
            self.last_error = Some(
                message
                    .error_message
                    .clone()
                    .unwrap_or_else(|| "pi reported an error".to_string()),
            );
        }
        let mut out = Vec::new();
        if let Some(usage) = message
            .usage
            .and_then(|u| u.into_normalized(message.model.clone()))
        {
            out.push(NormalizedEvent::TokenUsage { usage });
        }
        let content = message
            .content
            .into_iter()
            .enumerate()
            .filter_map(|(index, block)| block.into_normalized_content(&item_id, index))
            .collect();
        out.push(NormalizedEvent::AssistantMessage {
            partial: false,
            stop_reason,
            content,
        });
        out
    }

    fn current_item_id(&self) -> String {
        stable_id(&format!("pi-msg-{}", self.message_index.max(1)), "msg")
    }
}

/// Map pi's stop reasons onto the Anthropic vocabulary the turn normalizer
/// phases on (`tool_use` = commentary, `end_turn` = final answer). `error` and
/// `aborted` stay unphased.
fn normalize_stop_reason(reason: &str) -> &str {
    match reason {
        "stop" => "end_turn",
        "toolUse" => "tool_use",
        "length" => "max_tokens",
        other => other,
    }
}

fn reasoning_item_id(item_id: &str, index: usize) -> String {
    format!("{item_id}-reasoning-{index}")
}

impl PiContentBlock {
    fn into_normalized_content(self, item_id: &str, index: usize) -> Option<NormalizedContent> {
        match self {
            Self::Text { text } => Some(NormalizedContent::AgentText {
                item_id: item_id.to_string(),
                text,
            }),
            Self::Thinking { thinking, text } => Some(NormalizedContent::ReasoningText {
                item_id: reasoning_item_id(item_id, index),
                text: thinking.or(text).unwrap_or_default(),
            }),
            Self::ToolCall {
                id,
                name,
                arguments,
            } => Some(NormalizedContent::ToolUse {
                raw_id: id.unwrap_or_else(|| "tool".to_string()),
                tool: name.unwrap_or_else(|| "tool".to_string()),
                arguments: arguments.unwrap_or_else(|| json!({})),
            }),
            Self::Unknown => None,
        }
    }
}

/// Extract display text and an exit code from a pi `tool_execution_end`
/// result. Tool results carry `content` blocks (what the model saw); the bash
/// tool additionally reports `structuredContent.output`/`exitCode`, which wins
/// when present so command-execution rendering gets the real exit code.
fn tool_result_content(result: Option<&Value>) -> (String, Option<i32>) {
    let Some(result) = result else {
        return (String::new(), None);
    };
    let structured = result.get("structuredContent");
    let exit_code = structured
        .and_then(|value| value.get("exitCode"))
        .and_then(Value::as_i64)
        .and_then(|code| i32::try_from(code).ok());
    let content_text = result
        .get("content")
        .and_then(Value::as_array)
        .map(|blocks| {
            blocks
                .iter()
                .filter_map(|block| block.get("text").and_then(Value::as_str))
                .collect::<Vec<_>>()
                .join("")
        })
        .filter(|text| !text.is_empty());
    let output = content_text.or_else(|| {
        structured
            .and_then(|value| value.get("output"))
            .and_then(Value::as_str)
            .map(str::to_string)
    });
    (output.unwrap_or_default(), exit_code)
}

#[cfg(test)]
mod tests {
    use base64::Engine;
    use codex_app_server_protocol::UserInput;
    use serde_json::{Value, json};

    use crate::{HarnessServer, NormalizedContent, NormalizedEvent};

    use super::{PiEvent, PiEventNormalizer, PiHarness};

    fn normalize(normalizer: &mut PiEventNormalizer, event: Value) -> Vec<NormalizedEvent> {
        normalizer.normalize(serde_json::from_value(event).unwrap())
    }

    fn normalize_line(normalizer: &mut PiEventNormalizer, line: &str) -> Vec<NormalizedEvent> {
        let event: PiEvent = serde_json::from_str(line).unwrap();
        normalizer.normalize(event)
    }

    #[test]
    fn system_message_with_string_content_parses() {
        let mut normalizer = PiEventNormalizer::default();
        assert!(matches!(
            normalize(
                &mut normalizer,
                json!({"type": "message_start", "message": {"role": "system", "content": ""}}),
            )
            .as_slice(),
            [NormalizedEvent::Ignored]
        ));
    }

    #[test]
    fn turn_stdin_is_an_rpc_prompt_command() {
        let bytes = PiHarness
            .stdin_for_turn(&[UserInput::Text {
                text: "hello".to_string(),
                text_elements: Vec::new(),
            }])
            .unwrap();
        let value: Value = serde_json::from_slice(&bytes).unwrap();

        assert_eq!(value["type"], "prompt");
        assert_eq!(value["message"], "hello");
        assert!(value.get("images").is_none());
    }

    #[test]
    fn steer_stdin_is_an_rpc_steer_command() {
        let bytes = PiHarness
            .stdin_for_steer(&[UserInput::Text {
                text: "new guidance".to_string(),
                text_elements: Vec::new(),
            }])
            .unwrap();
        let value: Value = serde_json::from_slice(&bytes).unwrap();

        assert_eq!(value["type"], "steer");
        assert_eq!(value["message"], "new guidance");
    }

    #[test]
    fn local_image_input_inlines_base64_into_the_prompt_command() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!(
            "pi-harness-test-{}.png",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::write(&path, b"png-bytes").unwrap();

        let bytes = PiHarness
            .stdin_for_turn(&[
                UserInput::Text {
                    text: "what is this?".to_string(),
                    text_elements: Vec::new(),
                },
                UserInput::LocalImage {
                    path: path.clone(),
                    detail: None,
                },
            ])
            .unwrap();
        std::fs::remove_file(&path).ok();
        let value: Value = serde_json::from_slice(&bytes).unwrap();

        assert!(value["message"].as_str().unwrap().contains("what is this?"));
        assert_eq!(value["images"][0]["type"], "image");
        assert_eq!(value["images"][0]["mimeType"], "image/png");
        assert_eq!(
            value["images"][0]["data"],
            base64::engine::general_purpose::STANDARD.encode(b"png-bytes")
        );
    }

    #[test]
    fn text_deltas_stream_then_message_end_completes() {
        let mut normalizer = PiEventNormalizer::default();

        let events = normalize(
            &mut normalizer,
            json!({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}}),
        );
        assert!(
            events
                .iter()
                .all(|event| matches!(event, NormalizedEvent::Ignored))
        );

        let events = normalize(
            &mut normalizer,
            json!({"type": "message_update", "usage": {}, "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "PO"}}),
        );
        assert!(matches!(
            events.as_slice(),
            [NormalizedEvent::AgentTextDelta { item_id, delta }]
                if item_id == "pi-msg-1" && delta == "PO"
        ));

        let events = normalize(
            &mut normalizer,
            json!({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "model": "claude-sonnet-5", "usage": {"input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 4, "totalTokens": 15}, "content": [{"type": "text", "text": "PONG"}]}}),
        );
        let usage = events.iter().find_map(|event| match event {
            NormalizedEvent::TokenUsage { usage } => Some(usage),
            _ => None,
        });
        assert_eq!(usage.and_then(|u| u.input_tokens), Some(10));
        assert_eq!(usage.and_then(|u| u.cache_read_input_tokens), Some(3));
        assert!(events.iter().any(|event| matches!(
            event,
            NormalizedEvent::AssistantMessage {
                partial: false,
                stop_reason: Some(reason),
                ..
            } if reason == "end_turn"
        )));
    }

    #[test]
    fn tool_use_and_tool_result_normalize_with_exit_code() {
        let mut normalizer = PiEventNormalizer::default();
        normalize(
            &mut normalizer,
            json!({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}}),
        );

        let events = normalize(
            &mut normalizer,
            json!({"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse", "content": [{"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "echo ok"}}]}}),
        );
        assert!(events.iter().any(|event| matches!(
            event,
            NormalizedEvent::AssistantMessage {
                partial: false,
                stop_reason: Some(reason),
                content,
            } if reason == "tool_use" && matches!(
                content.as_slice(),
                [NormalizedContent::ToolUse { raw_id, tool, .. }]
                    if raw_id == "call_1" && tool == "bash"
            )
        )));

        let events = normalize(
            &mut normalizer,
            json!({"type": "tool_execution_end", "toolCallId": "call_1", "toolName": "bash", "result": {"content": [{"type": "text", "text": "ok\n"}], "structuredContent": {"output": "ok\n", "exitCode": 0}}, "isError": false}),
        );
        assert!(events.iter().any(|event| matches!(
            event,
            NormalizedEvent::ToolResults(results)
                if results.len() == 1
                    && results[0].tool_use_id == "call_1"
                    && results[0].content == "ok\n"
                    && results[0].exit_code == Some(0)
                    && !results[0].is_error
        )));
    }

    #[test]
    fn agent_settled_is_terminal_and_carries_message_error() {
        let mut normalizer = PiEventNormalizer::default();
        normalize(
            &mut normalizer,
            json!({"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "model overloaded", "content": []}}),
        );

        let events = normalize(&mut normalizer, json!({"type": "agent_settled"}));
        assert!(matches!(
            events.as_slice(),
            [NormalizedEvent::Result { error: Some(error) }] if error == "model overloaded"
        ));
    }

    #[test]
    fn agent_settled_after_clean_turn_has_no_error() {
        let mut normalizer = PiEventNormalizer::default();
        let events = normalize_line(&mut normalizer, r#"{"type":"agent_settled"}"#);
        assert!(matches!(
            events.as_slice(),
            [NormalizedEvent::Result { error: None }]
        ));
    }

    #[test]
    fn failed_prompt_response_terminates_the_turn() {
        let mut normalizer = PiEventNormalizer::default();
        let events = normalize(
            &mut normalizer,
            json!({"type": "response", "command": "prompt", "success": false, "error": "already streaming"}),
        );
        assert!(matches!(
            events.as_slice(),
            [NormalizedEvent::Error { message }] if message == "already streaming"
        ));
    }

    #[test]
    fn successful_responses_and_unrelated_failures_are_ignored() {
        let mut normalizer = PiEventNormalizer::default();
        assert!(matches!(
            normalize(
                &mut normalizer,
                json!({"id": "1", "type": "response", "command": "prompt", "success": true, "data": {"disposition": "started"}}),
            )
            .as_slice(),
            [NormalizedEvent::Ignored]
        ));
        assert!(matches!(
            normalize(
                &mut normalizer,
                json!({"type": "response", "command": "steer", "success": false, "error": "not running"}),
            )
            .as_slice(),
            [NormalizedEvent::Ignored]
        ));
    }

    #[test]
    fn user_and_tool_result_messages_are_ignored() {
        let mut normalizer = PiEventNormalizer::default();
        for event in [
            json!({"type": "message_start", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}),
            json!({"type": "message_end", "message": {"role": "toolResult", "content": [{"type": "text", "text": "ok"}]}}),
            json!({"type": "turn_end", "message": {"role": "assistant", "content": []}, "toolResults": []}),
            json!({"type": "agent_end", "messages": [], "willRetry": false}),
        ] {
            assert!(matches!(
                normalize(&mut normalizer, event).as_slice(),
                [NormalizedEvent::Ignored]
            ));
        }
    }

    #[test]
    fn thinking_deltas_use_reasoning_item_ids() {
        let mut normalizer = PiEventNormalizer::default();
        normalize(
            &mut normalizer,
            json!({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}}),
        );
        let events = normalize(
            &mut normalizer,
            json!({"type": "message_update", "usage": {}, "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "hmm"}}),
        );
        assert!(matches!(
            events.as_slice(),
            [NormalizedEvent::ReasoningTextDelta { item_id, delta }]
                if item_id == "pi-msg-1-reasoning-0" && delta == "hmm"
        ));
    }

    #[test]
    fn distinct_assistant_messages_get_distinct_item_ids() {
        let mut normalizer = PiEventNormalizer::default();
        for _ in 0..2 {
            normalize(
                &mut normalizer,
                json!({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}}),
            );
        }
        let events = normalize(
            &mut normalizer,
            json!({"type": "message_update", "usage": {}, "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "x"}}),
        );
        assert!(matches!(
            events.as_slice(),
            [NormalizedEvent::AgentTextDelta { item_id, delta }]
                if item_id == "pi-msg-2" && delta == "x"
        ));
    }
}
