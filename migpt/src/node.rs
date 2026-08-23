use futures::FutureExt;
use neon::prelude::*;
use std::sync::{Arc, LazyLock, Mutex};
use tokio::sync::oneshot;

use crate::runtime::run_async;

/// JS 回调的最大等待时间：超过即返回 Err，避免 Rust 侧请求永久挂起
const CALL_FN_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);

pub struct NodeManager {
    channel: Arc<Mutex<Option<Channel>>>,
}

static INSTANCE: LazyLock<NodeManager> = LazyLock::new(NodeManager::new);

impl NodeManager {
    pub fn new() -> Self {
        Self {
            channel: Arc::new(Mutex::new(None)),
        }
    }

    pub fn instance() -> &'static Self {
        &INSTANCE
    }

    pub fn init(&self, mut cx: ModuleContext) {
        let channel = cx.channel();
        *self.channel.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(channel);
    }

    pub async fn call_fn<R, F, F2>(&self, key: &str, map_arg: F, map_res: F2) -> Result<R, String>
    where
        R: Send + 'static,
        F: for<'a> Fn(&mut TaskContext<'a>) -> Handle<'a, JsValue> + Send + 'static,
        F2: Fn(&mut TaskContext<'_>, Handle<'_, JsValue>) -> Result<R, String>
            + Send
            + Sync
            + 'static,
    {
        let channel = match self.channel.lock() {
            Ok(guard) => guard.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        };
        let channel = match channel {
            Some(channel) => channel,
            None => return Err("NodeManager 尚未初始化".into()),
        };

        let key = key.to_string();
        let key_for_cb = key.clone();  // 闭包用克隆，原 key 留给超时错误消息
        let (tx, rx) = oneshot::channel::<Result<R, String>>();

        // 事件循环已关闭时立即返回错误，而不是挂起等待
            // 发送失败（事件循环已关闭）由 rx 超时兜底，无需阻塞等待
            let _ = channel.send(move |mut cx| {
                let Ok(callbacks) = cx.global::<JsObject>("RUST_CALLBACKS") else {
                    let _ = tx.send(Err("无法获取 RUST_CALLBACKS 对象".into()));
                    return Ok(());
                };

                let Ok(callback) = callbacks.get::<JsFunction, _, _>(&mut cx, key_for_cb.as_str()) else {
                    let _ = tx.send(Err(format!("找不到函数: {}", key_for_cb)));
                    return Ok(());
                };

                let arg = map_arg(&mut cx);
                let this = cx.undefined();

                let Ok(res) = callback.call(&mut cx, this, [arg]) else {
                    let _ = tx.send(Err("函数调用失败".into()));
                    return Ok(());
                };

                if res.is_a::<JsPromise, _>(&mut cx) {
                    let Ok(promise) = res.downcast::<JsPromise, _>(&mut cx) else {
                        let _ = tx.send(Err("类型转换失败".into()));
                        return Ok(());
                    };

                    match promise.to_future(&mut cx, move |mut cx, res| match res {
                        Ok(res) => Ok(map_res(&mut cx, res)),
                        Err(err) => Ok(map_res(&mut cx, err)),
                    }) {
                        Ok(future) => {
                            run_async(async move {
                                // 捕获 JS 侧回调可能触发的 panic，避免污染 tokio 运行时。
                                // neon JoinHandle 输出为 Result<Result<R,String>, JoinError>，
                                // catch_unwind 再包一层。
                                let res = std::panic::AssertUnwindSafe(future).catch_unwind().await;
                                let _ = tx.send(match res {
                                    Ok(Ok(res)) => res,
                                    Ok(Err(e)) => Err(format!("JS 回调异常: {}", e)),
                                    Err(_) => Err("JS 回调 panic".into()),
                                });
                            });
                        }
                        Err(err) => {
                            let _ = tx.send(Err(format!("创建 promise future 失败: {}", err)));
                        }
                    }
                } else {
                    let _ = tx.send(map_res(&mut cx, res));
                }

                Ok(())
            });

        // 等待 JS 回调结果，30s 超时兜底
        match tokio::time::timeout(CALL_FN_TIMEOUT, rx).await {
            Ok(Ok(res)) => res,
            Ok(Err(_)) => Err("接收数据失败".into()),
            Err(_) => Err(format!(
                "JS 回调超时（{}s）: {}",
                CALL_FN_TIMEOUT.as_secs(),
                key
            )),
        }
    }
}
