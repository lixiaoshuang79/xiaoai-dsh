use neon::prelude::Context;
use neon::types::JsUint8Array;
use open_xiaoai::base::{AppError, VERSION};
use open_xiaoai::services::connect::data::{Event, Request, Response, Stream};
use open_xiaoai::services::connect::handler::MessageHandler;
use open_xiaoai::services::connect::message::{MessageManager, WsStream};
use open_xiaoai::services::connect::rpc::RPC;
use open_xiaoai::services::speaker::SpeakerManager;

use futures::{SinkExt, StreamExt};
use serde_json::json;
use std::collections::HashSet;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, Mutex as AsyncMutex};
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::{accept_async_with_config, WebSocketStream};

use crate::auth::{self, AuthMode};
use crate::node::NodeManager;

/// WS 握手超时
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);

/// 连接代际：每当一个「已完成握手 + 认证」的新连接就绪并接管时 +1。
/// 旧任务在 dispose 前复核自己的代际是否仍为当前代际，避免旧连接收尾时
/// 清掉新连接已 init 的 reader/writer（连接替换竞态）。
static CURRENT_GENERATION: AtomicU64 = AtomicU64::new(0);

/// 串行化 MessageManager 的 init/dispose 切换：同一时刻只有一个连接在做
/// 「收尾或初始化」过渡，杜绝新旧连接对全局单例的交替写入。
static MANAGER_LOCK: AsyncMutex<()> = AsyncMutex::const_new(());

/// 运行期认证配置（run() 启动时从环境变量读取一次）
#[derive(Clone)]
struct AuthConfig {
    mode: AuthMode,
    secret: String,
    allowlist: HashSet<String>,
}

impl AuthConfig {
    fn from_env() -> Self {
        let secret = std::env::var("XIAOAI_WS_SECRET").unwrap_or_default();
        let allowlist_raw = std::env::var("XIAOAI_WS_ALLOWLIST").unwrap_or_default();
        let auth_env = std::env::var("XIAOAI_WS_AUTH").ok();
        let mode = auth::resolve_auth_mode(
            if secret.is_empty() {
                None
            } else {
                Some(secret.as_str())
            },
            if allowlist_raw.trim().is_empty() {
                None
            } else {
                Some(allowlist_raw.as_str())
            },
            auth_env.as_deref(),
        );
        Self {
            mode,
            secret,
            allowlist: auth::parse_allowlist(&allowlist_raw),
        }
    }

    fn ip_allowed(&self, ip: &std::net::IpAddr) -> bool {
        self.allowlist.contains(&ip.to_string())
    }
}

pub struct AppServer;

/// 当前连接的句柄（主循环持有，替换时 abort）
struct CurrentConn {
    gen: u64,
    handle: tokio::task::JoinHandle<()>,
    test_handle: tokio::task::JoinHandle<()>,
}

impl AppServer {
    /// WS 握手（显式 64MiB 消息上限；应用层另有 512KiB/1MiB 限制在
    /// vendored message.rs 的 FrameLimiter 处执行）
    async fn accept(stream: TcpStream) -> Result<WebSocketStream<TcpStream>, AppError> {
        // WebSocketConfig 是 non_exhaustive，不能结构体字面量构造；用 Default + 字段赋值
        let mut config = WebSocketConfig::default();
        config.max_message_size = Some(64 << 20);
        let ws = accept_async_with_config(stream, Some(config)).await?;
        Ok(ws)
    }

    pub async fn run() -> Result<(), String> {
        let auth = AuthConfig::from_env();
        match auth.mode {
            AuthMode::Off => {
                if std::env::var("XIAOAI_WS_AUTH")
                    .map(|v| v.trim().eq_ignore_ascii_case("off"))
                    .unwrap_or(false)
                {
                    println!("⚠️ XIAOAI_WS_AUTH=off：WS 认证已显式关闭");
                } else {
                    println!("⚠️ 未配置 XIAOAI_WS_SECRET / XIAOAI_WS_ALLOWLIST：4399 端口对局域网全开放（仅建议内网使用）");
                }
            }
            AuthMode::IpOnly => {
                println!("🔐 认证模式: 仅来源 IP allowlist（{} 条）", auth.allowlist.len())
            }
            AuthMode::Secret => println!("🔐 认证模式: 共享 secret"),
            AuthMode::SecretAndIp => {
                println!(
                    "🔐 认证模式: allowlist（{} 条）+ 共享 secret",
                    auth.allowlist.len()
                )
            }
        }

        let bind = std::env::var("XIAOAI_WS_BIND").unwrap_or_else(|_| "0.0.0.0:4399".to_string());
        let listener = TcpListener::bind(&bind)
            .await
            .map_err(|e| format!("❌ 绑定地址失败: {}: {}", bind, e))?;
        println!("✅ 已启动: {}", bind);

        let (tx, mut rx) = mpsc::channel::<(WebSocketStream<TcpStream>, SocketAddr)>(8);
        let mut current: Option<CurrentConn> = None;

        loop {
            tokio::select! {
                res = listener.accept() => match res {
                    Ok((stream, addr)) => {
                        // 来源 IP allowlist：accept 后立即断开，不做握手
                        if auth.mode.requires_allowlist() && !auth.ip_allowed(&addr.ip()) {
                            println!("🚫 拒绝来源: {:?}（不在 allowlist）", addr);
                            drop(stream);
                            continue;
                        }
                        let tx = tx.clone();
                        let auth = auth.clone();
                        tokio::spawn(async move {
                            connector(stream, addr, auth, tx).await;
                        });
                    }
                    Err(e) => {
                        // accept 出错（如 EMFILE）不退出服务，退避后继续
                        println!("❌ accept 错误: {}，500ms 后重试", e);
                        tokio::time::sleep(Duration::from_millis(500)).await;
                    }
                },
                Some((ws, addr)) = rx.recv() => {
                    // 仅「握手 + 认证全部通过」的连接才会走到这里：先换代再 abort 旧任务，
                    // 旧任务在 dispose 前复核代际会发现自己已过期而跳过收尾
                    let gen = CURRENT_GENERATION.fetch_add(1, Ordering::AcqRel) + 1;
                    if let Some(cur) = current.take() {
                        println!("🔄 新连接接管 (generation {} -> {})", cur.gen, gen);
                        cur.handle.abort();
                        cur.test_handle.abort();
                    }
                    let handle = tokio::spawn(async move {
                        AppServer::handle_connection(ws, addr, gen).await;
                    });
                    let test_handle = tokio::spawn(async move {
                        tokio::time::sleep(Duration::from_secs(1)).await;
                        let _ = announce_connected().await;
                    });
                    current = Some(CurrentConn { gen, handle, test_handle });
                }
            }
        }
    }

    async fn handle_connection(
        ws_stream: WebSocketStream<TcpStream>,
        addr: SocketAddr,
        gen: u64,
    ) {
        println!("✅ 已连接: {:?} (generation {})", addr, gen);
        // 认证已在 connector 完成；此处仅在互斥锁内初始化全局单例，
        // 串行化与上一连接收尾的交替
        {
            let _guard = MANAGER_LOCK.lock().await;
            MessageManager::instance().init(WsStream::Server(ws_stream)).await;
        }
        MessageHandler::<Event>::instance()
            .set_handler(on_event)
            .await;
        MessageHandler::<Stream>::instance()
            .set_handler(on_stream)
            .await;
        RPC::instance().add_command("get_version", get_version).await;

        if let Err(e) = MessageManager::instance().process_messages().await {
            println!("❌ 消息处理异常: {} ({})", e, addr);
        }
        teardown(gen).await;
        println!("❌ 已断开连接: {:?} (generation {})", addr, gen);
    }
}

/// 连接就绪前的过渡任务：握手 + 可选认证；全部通过才把连接交给主循环接管
/// （此阶段绝不 abort 旧连接、不注册 handler、不发业务数据）
async fn connector(
    stream: TcpStream,
    addr: SocketAddr,
    auth: AuthConfig,
    tx: mpsc::Sender<(WebSocketStream<TcpStream>, SocketAddr)>,
) {
    // WS 握手（10s 超时）；失败只记日志，不影响现有连接
    let handshake = tokio::time::timeout(HANDSHAKE_TIMEOUT, AppServer::accept(stream)).await;
    let Ok(Ok(mut ws)) = handshake else {
        println!("❌ 握手失败/超时: {:?}", addr);
        return;
    };

    // secret 认证：握手后 5s 内首条消息必须是 {"auth":"<secret>"}
    if auth.mode.requires_secret() {
        match authenticate(&mut ws, &auth.secret, auth::AUTH_TIMEOUT).await {
            Ok(()) => println!("🔑 认证成功: {:?}", addr),
            Err(e) => {
                // Box<dyn Error> 非 Send：立即消费成 String 并 drop，不能跨 await 存活
                let msg = format!("{}", e);
                drop(e);
                println!("❌ 认证失败: {:?}: {}", addr, msg);
                let _ = ws
                    .send(WsMessage::Text(r#"{"error":"auth_failed"}"#.into()))
                    .await;
                let _ = ws.send(WsMessage::Close(None)).await;
                return;
            }
        }
    }

    // 认证通过：交给主循环（主循环负责换代并 abort 旧连接）
    if tx.send((ws, addr)).await.is_err() {
        println!("❌ 主循环已退出，连接丢弃: {:?}", addr);
    }
}

/// 读取并校验首条认证消息。返回 Err 表示认证失败（调用方负责关闭连接并记日志）。
async fn authenticate(
    ws: &mut WebSocketStream<TcpStream>,
    secret: &str,
    timeout: Duration,
) -> Result<(), AppError> {
    let msg = tokio::time::timeout(timeout, ws.next())
        .await
        .map_err(|_| AppError::from(format!("认证超时（{}s 内未收到消息）", timeout.as_secs())))?
        .ok_or("连接在认证前关闭")?
        .map_err(|e| AppError::from(format!("读取认证消息失败: {}", e)))?;
    match msg {
        WsMessage::Text(text) if auth::verify_auth_message(&text, secret) => Ok(()),
        WsMessage::Text(_) => Err("认证信息不匹配".into()),
        _ => Err("首条消息不是文本消息".into()),
    }
}

/// 连接收尾：仅当自己仍是当前代际时才 dispose 全局单例（避免清掉新连接的读写端）；
/// 已被替换/过期的任务跳过收尾
async fn teardown(gen: u64) {
    let _guard = MANAGER_LOCK.lock().await;
    if teardown_should_dispose(gen, CURRENT_GENERATION.load(Ordering::Acquire)) {
        MessageManager::instance().dispose().await;
    } else {
        println!("⚠️ 代际已过期 ({} < current)，跳过 dispose", gen);
    }
}

/// 纯函数：代际仍为当前代际时才允许 dispose（可单测）
fn teardown_should_dispose(my_gen: u64, current_gen: u64) -> bool {
    my_gen == current_gen
}

/// 连接建立 1 秒后播报「已连接」（沿用原行为；失败静默）
async fn announce_connected() -> Result<(), AppError> {
    SpeakerManager::play_text("已连接").await?;
    Ok(())
}

async fn get_version(_: Request) -> Result<Response, AppError> {
    let data = json!(VERSION.to_string());
    Ok(Response::from_data(data))
}

async fn on_stream(stream: Stream) -> Result<(), AppError> {
    let Stream { tag, bytes, .. } = stream;
    match tag.as_str() {
        "record" => {
            NodeManager::instance()
                .call_fn::<(), _, _>(
                    "on_input_data",
                    move |cx| JsUint8Array::from_slice(cx, &bytes).unwrap().upcast(),
                    |_, _| Ok(()),
                )
                .await?;
        }
        _ => {}
    }
    Ok(())
}

async fn on_event(event: Event) -> Result<(), AppError> {
    let event_json = serde_json::to_string(&event)?;
    NodeManager::instance()
        .call_fn::<(), _, _>(
            "on_event",
            move |cx| cx.string(&event_json).upcast(),
            |_, _| Ok(()),
        )
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio_tungstenite::connect_async;

    /// 建立一对 loopback WS 连接（服务端走 AppServer::accept 的 64MiB 配置）
    async fn test_pair() -> (
        WebSocketStream<TcpStream>,
        tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<TcpStream>>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            AppServer::accept(stream).await.unwrap()
        });
        let (client, _) = connect_async(format!("ws://{}/", addr)).await.unwrap();
        let server = server.await.unwrap();
        (server, client)
    }

    #[test]
    fn ws_config_caps_message_size() {
        // WebSocketConfig 是 non_exhaustive，用 Default + 字段赋值
        let mut config = WebSocketConfig::default();
        config.max_message_size = Some(64 << 20);
        assert_eq!(config.max_message_size, Some(64 << 20));
    }

    #[test]
    fn teardown_only_current_generation_disposes() {
        assert!(teardown_should_dispose(3, 3));
        assert!(!teardown_should_dispose(2, 3));
        assert!(!teardown_should_dispose(4, 3));
    }

    #[test]
    fn auth_config_allowlist() {
        let cfg = AuthConfig {
            mode: AuthMode::IpOnly,
            secret: String::new(),
            allowlist: auth::parse_allowlist("192.0.2.10, 192.0.2.11"),
        };
        assert!(cfg.ip_allowed(&"192.0.2.10".parse().unwrap()));
        assert!(cfg.ip_allowed(&"192.0.2.11".parse().unwrap()));
        assert!(!cfg.ip_allowed(&"192.0.2.99".parse().unwrap()));
        assert!(!cfg.ip_allowed(&"203.0.113.1".parse().unwrap()));
    }

    #[tokio::test]
    async fn auth_success_consumes_first_message() {
        let (mut server, mut client) = test_pair().await;
        client
            .send(WsMessage::Text(r#"{"auth":"s3cret"}"#.into()))
            .await
            .unwrap();
        let res = authenticate(&mut server, "s3cret", Duration::from_secs(2)).await;
        assert!(res.is_ok(), "认证应成功: {:?}", res);
    }

    #[tokio::test]
    async fn auth_wrong_secret_fails() {
        let (mut server, mut client) = test_pair().await;
        client
            .send(WsMessage::Text(r#"{"auth":"wrong"}"#.into()))
            .await
            .unwrap();
        let res = authenticate(&mut server, "s3cret", Duration::from_secs(2)).await;
        assert!(res.is_err());
    }

    #[tokio::test]
    async fn auth_binary_first_message_fails() {
        let (mut server, mut client) = test_pair().await;
        client
            .send(WsMessage::Binary(vec![1, 2, 3].into()))
            .await
            .unwrap();
        let res = authenticate(&mut server, "s3cret", Duration::from_secs(2)).await;
        assert!(res.is_err());
    }

    #[tokio::test]
    async fn auth_timeout_fails() {
        let (mut server, _client) = test_pair().await;
        // 客户端不发任何消息，短超时触发
        let res = authenticate(&mut server, "s3cret", Duration::from_millis(200)).await;
        assert!(res.is_err());
        assert!(res.unwrap_err().to_string().contains("认证超时"));
    }

    #[tokio::test]
    async fn oversized_message_rejected_by_ws_config() {
        // 验证 WebSocket 层消息上限生效：服务端用 1KiB 上限，客户端发 2KiB 文本
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut config = WebSocketConfig::default();
            config.max_message_size = Some(1024);
            accept_async_with_config(stream, Some(config)).await.unwrap()
        });
        let (mut client, _) = connect_async(format!("ws://{}/", addr)).await.unwrap();
        let mut server = server.await.unwrap();
        let big = "x".repeat(2048);
        client.send(WsMessage::Text(big.into())).await.unwrap();
        // 服务端读取应报协议错误（消息超限）
        let res = tokio::time::timeout(Duration::from_secs(2), server.next()).await;
        assert!(res.is_ok());
        assert!(res.unwrap().unwrap().is_err());
    }
}
