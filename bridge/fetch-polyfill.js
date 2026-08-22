// 为 Node 17 提供全局 fetch（Node 18 才原生支持）
const https = require('https');
const http = require('http');

function fetchPolyfill(url, opts) {
  opts = opts || {};
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === 'https:' ? https : http;
    const headers = opts.headers || {};
    const req = mod.request({
      hostname: u.hostname,
      port: u.port || (u.protocol === 'https:' ? 443 : 80),
      path: u.pathname + u.search,
      method: (opts.method || 'GET').toUpperCase(),
      headers: headers,
    }, (res) => {
      let data = Buffer.alloc(0);
      res.on('data', (c) => { data = Buffer.concat([data, c]); });
      res.on('end', () => {
        const text = data.toString('utf8');
        resolve({
          status: res.statusCode,
          ok: res.statusCode >= 200 && res.statusCode < 300,
          text: async () => text,
          json: async () => JSON.parse(text),
        });
      });
    });
    req.on('error', reject);
    if (opts.body) req.write(opts.body);
    req.end();
  });
}

globalThis.fetch = fetchPolyfill;
module.exports = fetchPolyfill;
