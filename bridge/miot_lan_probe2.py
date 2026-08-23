#!/usr/bin/env python3
"""MIoT LAN 直连验证 v2：OT_PROBE 握手 → 识别 did/IP → 带正确 stamp 的 RPC。

用法:
  python3 miot_lan_probe2.py <设备did> <HA设备字典文件路径>
  例: python3 miot_lan_probe2.py 100000001 \\
      ~/path/to/ha/config/.storage/xiaomi_home/miot_devices/100000001_cn.dict
"""
import json, socket, struct, time, re, sys
from hashlib import md5 as _md5
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

DID = sys.argv[1] if len(sys.argv) > 1 else "100000001"
TOKEN_DICT_PATH = sys.argv[2] if len(sys.argv) > 2 else ""

def load_token(did):
    data = open(TOKEN_DICT_PATH, 'rb').read()
    s = data.decode('utf-8', errors='replace')
    i = s.find('"' + did + '"')
    m = re.search(r'"token":\s*"([0-9a-f]{32})"', s[i:i+800])
    return m.group(1)

def make_cipher(token_hex):
    token = bytes.fromhex(token_hex)
    key = _md5(token).digest(); iv = _md5(key + token).digest()
    return Cipher(algorithms.AES128(key), modes.CBC(iv))

def encrypt(cipher, obj):
    padder = padding.PKCS7(128).padder()
    raw = padder.update(json.dumps(obj).encode()) + padder.finalize()
    e = cipher.encryptor()
    return e.update(raw) + e.finalize()

def decrypt(cipher, raw):
    d = cipher.decryptor()
    raw = d.update(raw) + d.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    raw = unpadder.update(raw) + unpadder.finalize()
    return json.loads(raw.rstrip(b'\x00'))

def main():
    token_hex = load_token(DID)
    token = bytes.fromhex(token_hex)
    cipher = make_cipher(token_hex)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 0))

    # 1. OT_PROBE 广播：32 字节 = 2131 0020 ffff... 'MDID' + virtual_did(8B)
    probe = bytearray(32)
    probe[:20] = b'!1\x00\x20\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFFMDID'
    probe[20:28] = struct.pack('>Q', 0x5A5A5A5A5A5A5A5A)
    probe[28:32] = b'\x00\x00\x00\x00'
    sock.sendto(bytes(probe), ("255.255.255.255", 54321))

    # 2. 收集响应，找插座的 IP 与设备时间戳
    target_ip = None
    device_ts = None
    sock.settimeout(4)
    t0 = time.time()
    responses = []
    while time.time() - t0 < 4:
        try:
            data, addr = sock.recvfrom(4096)
            if len(data) < 16:
                continue
            if data[:2] == b'\x21\x31' and len(data) == 32:
                did = struct.unpack('>Q', data[4:12])[0]
                ts = struct.unpack('>I', data[12:16])[0]
                responses.append((addr[0], did, ts))
        except socket.timeout:
            break
    print("[probe responses]:")
    for ip, did, ts in responses:
        print(f"  {ip} did={did} dev_ts={ts}")
        if str(did) == DID:
            target_ip, device_ts = ip, ts
    if not target_ip:
        print("!! 插座没有响应 probe（设备不在线或已休眠）")
        return

    # 3. RPC：stamp = now - (now - device_ts) = device_ts 直接作为 stamp
    offset = int(time.time()) - device_ts
    stamp = int(time.time()) - offset
    print(f"[rpc] target={target_ip} device_ts={device_ts} offset={offset} stamp={stamp}")
    body = encrypt(cipher, {"id": 123, "method": "get_properties",
                            "params": [{"did": DID, "siid": 4, "piid": 1}]})
    total = 32 + len(body)
    hdr = struct.pack('>HHQI16s', 0x2131, total, 0, stamp, token)
    pkt = bytearray(hdr + body)
    pkt[16:32] = _md5(pkt[:16] + token + pkt[32:]).digest()
    sock.sendto(bytes(pkt), (target_ip, 54321))
    sock.settimeout(6)
    t1 = time.time()
    while time.time() - t1 < 6:
        try:
            data, _ = sock.recvfrom(4096)
            if len(data) <= 32:
                continue
            try:
                result = decrypt(cipher, data[32:])
                print("[rpc] indicator-light =>", json.dumps(result, ensure_ascii=False))
                return
            except Exception as e:
                print(f"[rpc] decrypt err {e}, len={len(data)}")
        except socket.timeout:
            break
    print("[rpc] no response")

if __name__ == "__main__":
    main()
