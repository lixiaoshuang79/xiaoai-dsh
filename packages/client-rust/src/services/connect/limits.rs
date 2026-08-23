//! 应用层消息大小限制与 record 音频帧令牌桶。
//!
//! 背景：WebSocket 层（tungstenite）的 `max_message_size` 默认 64MiB，单帧可以很大；
//! 服务端（migpt）必须在此之上再做应用层限制，防止异常/恶意客户端用超大文本帧或
//! 音频帧打满内存、CPU 与 JS 回调通道。限制策略：
//! - 文本消息 ≤ 1MiB，二进制消息 ≤ 512KiB（音频帧按此上限）；
//! - `record` 标签的音频帧额外走令牌桶（容量/速率见下），杜绝灌流刷爆 on_input_data；
//! - 超限计次，达到 [`MAX_VIOLATIONS`] 次后由调用方断开连接。
//!
//! `FrameLimiter` 由 `MessageManager::process_messages` 每次连接创建，天然按连接隔离。

use std::time::Instant;

use crate::base::AppError;

/// 文本消息最大长度（1MiB）
pub const MAX_TEXT_SIZE: usize = 1 << 20;
/// 二进制消息（含 record 音频帧）最大长度（512KiB）
pub const MAX_BINARY_SIZE: usize = 512 << 10;
/// 累计超限多少次后断开连接
pub const MAX_VIOLATIONS: u32 = 3;
/// record 音频帧令牌桶：容量（突发余量，约 2 秒满速音频）
pub const RECORD_BUCKET_CAPACITY: u64 = 1 << 20;
/// record 音频帧令牌桶：补充速率（512KiB/s，覆盖 48kHz 立体声 16bit 的 192KiB/s 与
/// 16kHz 单声道 16bit 的 32KiB/s，留 2 倍以上余量）
pub const RECORD_REFILL_PER_SEC: u64 = 512 << 10;

/// 消息超限状态机。每次 `process_messages` 调用新建一个实例（按连接隔离）。
#[derive(Debug)]
pub struct FrameLimiter {
    violations: u32,
    record_tokens: u64,
    record_last_refill: Instant,
}

impl Default for FrameLimiter {
    fn default() -> Self {
        Self::new()
    }
}

impl FrameLimiter {
    pub fn new() -> Self {
        Self {
            violations: 0,
            record_tokens: RECORD_BUCKET_CAPACITY,
            record_last_refill: Instant::now(),
        }
    }

    /// 检查一条文本消息的长度；超限计次，达到上限返回 Err（调用方断开连接）。
    pub fn check_text(&mut self, len: usize) -> Result<(), AppError> {
        self.check(len, MAX_TEXT_SIZE, "text")
    }

    /// 检查一条二进制消息的长度；超限计次，达到上限返回 Err（调用方断开连接）。
    pub fn check_binary(&mut self, len: usize) -> Result<(), AppError> {
        self.check(len, MAX_BINARY_SIZE, "binary")
    }

    /// 检查一条 `record` 音频帧是否落在令牌桶额度内。
    pub fn check_record_frame(&mut self, len: usize) -> Result<(), AppError> {
        let len = len as u64;
        let now = Instant::now();
        let elapsed = now.duration_since(self.record_last_refill).as_secs_f64();
        self.record_last_refill = now;
        // 先按流逝时间补充令牌（不超过容量）
        let refill = (elapsed * RECORD_REFILL_PER_SEC as f64) as u64;
        self.record_tokens = (self.record_tokens + refill).min(RECORD_BUCKET_CAPACITY);

        if len > self.record_tokens {
            self.violations += 1;
            if self.violations >= MAX_VIOLATIONS {
                return Err(format!(
                    "record 音频帧超速（{} bytes > 令牌桶 {} bytes），断开连接",
                    len, self.record_tokens
                )
                .into());
            }
            return Err(format!(
                "record 音频帧超速（{} bytes > 令牌桶 {} bytes）",
                len, self.record_tokens
            )
            .into());
        }
        self.record_tokens -= len;
        Ok(())
    }

    /// 超限计次；达到 [`MAX_VIOLATIONS`] 返回 Err（调用方断开连接）。
    fn check(&mut self, len: usize, limit: usize, kind: &str) -> Result<(), AppError> {
        if len <= limit {
            return Ok(());
        }
        self.violations += 1;
        if self.violations >= MAX_VIOLATIONS {
            return Err(format!(
                "{} 消息超限（{} bytes > {} bytes）达到 {} 次，断开连接",
                kind, len, limit, self.violations
            )
            .into());
        }
        Err(format!(
            "{} 消息超限（{} bytes > {} bytes）",
            kind, len, limit
        )
        .into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_within_limit_ok() {
        let mut limiter = FrameLimiter::new();
        assert!(limiter.check_text(MAX_TEXT_SIZE).is_ok());
        assert!(limiter.check_text(0).is_ok());
        assert_eq!(limiter.violations, 0);
    }

    #[test]
    fn text_over_limit_counts_and_disconnects() {
        let mut limiter = FrameLimiter::new();
        // 前两次超限只计数
        assert!(limiter.check_text(MAX_TEXT_SIZE + 1).is_err());
        assert!(limiter.check_text(MAX_TEXT_SIZE + 1).is_err());
        assert_eq!(limiter.violations, 2);
        // 第三次超限要求断开
        assert!(limiter.check_text(MAX_TEXT_SIZE + 1).is_err());
        assert_eq!(limiter.violations, 3);
    }

    #[test]
    fn binary_within_limit_ok() {
        let mut limiter = FrameLimiter::new();
        assert!(limiter.check_binary(MAX_BINARY_SIZE).is_ok());
    }

    #[test]
    fn binary_over_limit_disconnects_after_max_violations() {
        let mut limiter = FrameLimiter::new();
        for _ in 0..MAX_VIOLATIONS - 1 {
            assert!(limiter.check_binary(MAX_BINARY_SIZE + 1).is_err());
        }
        let err = limiter.check_binary(MAX_BINARY_SIZE + 1).unwrap_err();
        assert!(err.to_string().contains("断开连接"));
    }

    #[test]
    fn record_frame_within_bucket_ok() {
        let mut limiter = FrameLimiter::new();
        // 满桶容量内一帧直接放行
        assert!(limiter.check_record_frame(1024).is_ok());
        assert!(limiter.check_record_frame(RECORD_BUCKET_CAPACITY as usize - 1024).is_ok());
    }

    #[test]
    fn record_frame_over_bucket_violates() {
        let mut limiter = FrameLimiter::new();
        // 单帧超过整桶容量：必然超速
        assert!(limiter.check_record_frame((RECORD_BUCKET_CAPACITY + 1) as usize).is_err());
    }

    #[test]
    fn record_burst_disconnects_after_max_violations() {
        let mut limiter = FrameLimiter::new();
        for _ in 0..MAX_VIOLATIONS - 1 {
            assert!(limiter.check_record_frame(RECORD_BUCKET_CAPACITY as usize + 1).is_err());
        }
        let err = limiter.check_record_frame(RECORD_BUCKET_CAPACITY as usize + 1).unwrap_err();
        assert!(err.to_string().contains("断开连接"));
    }
}
