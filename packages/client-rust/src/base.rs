// 修复（vendored 补丁）：原版 `Box<dyn Error>` 非 Send，异步 future 无法跨
// tokio::spawn；加 Send + Sync 后 API 完全兼容（From<String>/&str/Display/? 均不变）。
pub type AppError = Box<dyn std::error::Error + Send + Sync>;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
