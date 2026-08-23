//! WS 连接认证（纯逻辑模块，不依赖 neon，可单测）。
//!
//! 两级认证，均可选（由环境变量配置，TS 侧注入）：
//! - 来源 IP allowlist（XIAOAI_WS_ALLOWLIST，逗号分隔）：accept 后立即检查，不在列表直接断开；
//! - 共享 secret（XIAOAI_WS_SECRET）：WS 握手后 [AUTH_TIMEOUT] 内首条文本消息必须是
//!   `{"auth":"<secret>"}`，constant-time 比较。
//!
//! 认证模式由 [AuthMode] 描述，见 [resolve_auth_mode]。未配置任何认证时保持旧行为
//! （0.0.0.0 全开放），仅打印警告。

use std::collections::HashSet;
use std::time::Duration;

/// 握手后等待首条认证消息的超时（秒）
pub const AUTH_TIMEOUT_SECS: u64 = 5;
/// 握手后等待首条认证消息的超时
pub const AUTH_TIMEOUT: Duration = Duration::from_secs(AUTH_TIMEOUT_SECS);

/// 认证模式
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthMode {
    /// 完全关闭（XIAOAI_WS_AUTH=off，仅警告）
    Off,
    /// 仅来源 IP allowlist
    IpOnly,
    /// 仅共享 secret
    Secret,
    /// allowlist + secret 双重要求
    SecretAndIp,
}

impl AuthMode {
    pub fn requires_secret(&self) -> bool {
        matches!(self, AuthMode::Secret | AuthMode::SecretAndIp)
    }

    pub fn requires_allowlist(&self) -> bool {
        matches!(self, AuthMode::IpOnly | AuthMode::SecretAndIp)
    }
}

/// 根据环境变量解析认证模式。
///
/// - `auth_env`（XIAOAI_WS_AUTH）为 "off" 时强制关闭（仅警告）；
/// - 否则按配置自动组合：有 secret 则要求 secret，有 allowlist 则要求 allowlist；
/// - 两者都没有 → Off（保持兼容）。
pub fn resolve_auth_mode(
    secret: Option<&str>,
    allowlist: Option<&str>,
    auth_env: Option<&str>,
) -> AuthMode {
    let secret_configured = secret.map(|s| !s.is_empty()).unwrap_or(false);
    let allowlist_configured = allowlist.map(|s| !s.trim().is_empty()).unwrap_or(false);

    if auth_env.map(|s| s.trim().eq_ignore_ascii_case("off")).unwrap_or(false) {
        return AuthMode::Off;
    }
    match (secret_configured, allowlist_configured) {
        (true, true) => AuthMode::SecretAndIp,
        (true, false) => AuthMode::Secret,
        (false, true) => AuthMode::IpOnly,
        (false, false) => AuthMode::Off,
    }
}

/// 解析逗号分隔的 IP allowlist（容忍空白）。
pub fn parse_allowlist(raw: &str) -> HashSet<String> {
    raw.split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// 从首条消息文本中解析认证 secret。
///
/// 格式：`{"auth":"<secret>"}`（对齐 open-xiaoai 的 JSON 消息风格；允许附带其他字段，
/// 以便未来扩展不破坏兼容）。
pub fn parse_auth_message(text: &str) -> Option<String> {
    if text.len() > 4096 {
        return None;
    }
    let value: serde_json::Value = serde_json::from_str(text).ok()?;
    let obj = value.as_object()?;
    match obj.get("auth") {
        Some(serde_json::Value::String(secret)) => Some(secret.clone()),
        _ => None,
    }
}

/// constant-time 字符串比较（先比长度，再逐字节 XOR）。
pub fn constant_time_eq(a: &str, b: &str) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.bytes().zip(b.bytes()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// 校验一条首条消息是否为合法的认证消息。
pub fn verify_auth_message(text: &str, secret: &str) -> bool {
    match parse_auth_message(text) {
        Some(provided) => constant_time_eq(&provided, secret),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_off_env_wins() {
        assert_eq!(
            resolve_auth_mode(Some("s"), Some("192.0.2.4"), Some("off")),
            AuthMode::Off
        );
        assert_eq!(
            resolve_auth_mode(Some("s"), Some("192.0.2.4"), Some(" OFF ")),
            AuthMode::Off
        );
    }

    #[test]
    fn resolve_mode_combinations() {
        assert_eq!(resolve_auth_mode(None, None, None), AuthMode::Off);
        assert_eq!(resolve_auth_mode(Some(""), None, None), AuthMode::Off);
        assert_eq!(resolve_auth_mode(Some("s"), None, None), AuthMode::Secret);
        assert_eq!(resolve_auth_mode(None, Some("192.0.2.4"), None), AuthMode::IpOnly);
        assert_eq!(
            resolve_auth_mode(Some("s"), Some("192.0.2.4"), None),
            AuthMode::SecretAndIp
        );
        // 空字符串 allowlist 视为未配置
        assert_eq!(
            resolve_auth_mode(Some("s"), Some("  "), None),
            AuthMode::Secret
        );
    }

    #[test]
    fn parse_allowlist_handles_whitespace_and_empty() {
        let set = parse_allowlist("192.0.2.10, 192.0.2.11 ,,  ,");
        assert_eq!(set.len(), 2);
        assert!(set.contains("192.0.2.10"));
        assert!(set.contains("192.0.2.11"));
        assert!(parse_allowlist("").is_empty());
        assert!(parse_allowlist(" , , ").is_empty());
    }

    #[test]
    fn parse_auth_message_accepts_valid() {
        assert_eq!(
            parse_auth_message(r#"{"auth":"abc123"}"#).as_deref(),
            Some("abc123")
        );
        // 允许附带额外字段
        assert_eq!(
            parse_auth_message(r#"{"auth":"x","type":"auth"}"#).as_deref(),
            Some("x")
        );
    }

    #[test]
    fn parse_auth_message_rejects_invalid() {
        assert_eq!(parse_auth_message("not json"), None);
        assert_eq!(parse_auth_message(r#"{"auth":123}"#), None);
        assert_eq!(parse_auth_message(r#"{"secret":"abc"}"#), None);
        assert_eq!(parse_auth_message(r#"[]"#), None);
        assert_eq!(parse_auth_message(""), None);
        // 超长文本直接拒绝（防滥用解析器）
        let long = format!(r#"{{"auth":"{}"}}"#, "a".repeat(5000));
        assert_eq!(parse_auth_message(&long), None);
    }

    #[test]
    fn constant_time_eq_behaves() {
        assert!(constant_time_eq("abc", "abc"));
        assert!(!constant_time_eq("abc", "abd"));
        assert!(!constant_time_eq("abc", "abcd"));
        assert!(!constant_time_eq("abc", ""));
        assert!(constant_time_eq("", ""));
    }

    #[test]
    fn verify_auth_message_matches() {
        assert!(verify_auth_message(r#"{"auth":"s3cret"}"#, "s3cret"));
        assert!(!verify_auth_message(r#"{"auth":"wrong"}"#, "s3cret"));
        assert!(!verify_auth_message(r#"{"auth":"s3cret"}"#, "s3creT"));
        assert!(!verify_auth_message("garbage", "s3cret"));
    }
}
