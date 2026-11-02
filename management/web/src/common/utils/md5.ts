/**
 * 极简 MD5 实现（无第三方依赖，可在 Web Worker / 主线程 / Node 里跑）。
 *
 * 为什么不用 crypto.subtle：Web Crypto 不支持 MD5，而 MinIO 的 `Content-MD5`
 * 要求的就是 MD5（base64）。用 spark-md5 之类也可以，但这里只需要“对单个分片
 * 做一次哈希”，自带实现可以少一个依赖。
 */

const SHIFT = [
  7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9, 14, 20, 5,
  9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11,
  16, 23, 4, 11, 16, 23, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15,
  21,
];

// K[i] = floor(abs(sin(i + 1)) * 2^32)
const K = new Int32Array(64);
for (let i = 0; i < 64; i += 1) {
  K[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296) | 0;
}

const B64_CHARS =
  'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

/**
 * 把 16 字节摘要转 base64（不依赖 btoa，Worker / Node 都能用）。
 * @param {Uint8Array} bytes
 * @returns {string}
 */
function bytesToBase64(bytes: Uint8Array): string {
  let out = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const b0 = bytes[i];
    const b1 = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const b2 = i + 2 < bytes.length ? bytes[i + 2] : 0;
    out += B64_CHARS[b0 >> 2];
    out += B64_CHARS[((b0 & 0x03) << 4) | (b1 >> 4)];
    out +=
      i + 1 < bytes.length ? B64_CHARS[((b1 & 0x0f) << 2) | (b2 >> 6)] : '=';
    out += i + 2 < bytes.length ? B64_CHARS[b2 & 0x3f] : '=';
  }
  return out;
}

function processBlocks(
  state: Int32Array,
  input: Uint8Array,
  offset: number,
  length: number,
) {
  const view = new DataView(input.buffer, input.byteOffset, input.byteLength);
  const m = new Int32Array(16);

  for (let block = offset; block < offset + length; block += 64) {
    for (let i = 0; i < 16; i += 1) {
      m[i] = view.getInt32(block + i * 4, true);
    }

    let a = state[0];
    let b = state[1];
    let c = state[2];
    let d = state[3];

    for (let i = 0; i < 64; i += 1) {
      let f;
      let g;
      if (i < 16) {
        f = (b & c) | (~b & d);
        g = i;
      } else if (i < 32) {
        f = (d & b) | (~d & c);
        g = (5 * i + 1) % 16;
      } else if (i < 48) {
        f = b ^ c ^ d;
        g = (3 * i + 5) % 16;
      } else {
        f = c ^ (b | ~d);
        g = (7 * i) % 16;
      }

      const sum = (a + f + K[i] + m[g]) | 0;
      const rotated = (sum << SHIFT[i]) | (sum >>> (32 - SHIFT[i])) | 0;
      a = d;
      d = c;
      c = b;
      b = (b + rotated) | 0;
    }

    state[0] = (state[0] + a) | 0;
    state[1] = (state[1] + b) | 0;
    state[2] = (state[2] + c) | 0;
    state[3] = (state[3] + d) | 0;
  }
}

/**
 * 可增量更新的 MD5 上下文。
 *
 * 整文件秒传不能把 1GB 一次性读进内存，因此按块 update，最后 digest。
 */
export class Md5Context {
  private state = new Int32Array([
    0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476,
  ]);

  private buffer = new Uint8Array(64);

  private bufferLength = 0;

  private totalBytes = 0;

  update(input: Uint8Array) {
    this.totalBytes += input.length;
    let offset = 0;

    if (this.bufferLength > 0) {
      const take = Math.min(64 - this.bufferLength, input.length);
      this.buffer.set(input.subarray(0, take), this.bufferLength);
      this.bufferLength += take;
      offset += take;
      if (this.bufferLength === 64) {
        processBlocks(this.state, this.buffer, 0, 64);
        this.bufferLength = 0;
      }
    }

    const fullLength = Math.floor((input.length - offset) / 64) * 64;
    if (fullLength > 0) {
      processBlocks(this.state, input, offset, fullLength);
      offset += fullLength;
    }

    if (offset < input.length) {
      this.buffer.set(input.subarray(offset), 0);
      this.bufferLength = input.length - offset;
    }
  }

  digest(): Uint8Array {
    const padded = new Uint8Array(this.bufferLength < 56 ? 64 : 128);
    padded.set(this.buffer.subarray(0, this.bufferLength));
    padded[this.bufferLength] = 0x80;

    const view = new DataView(padded.buffer);
    const bitLength = this.totalBytes * 8;
    view.setUint32(padded.length - 8, bitLength >>> 0, true);
    view.setUint32(padded.length - 4, Math.floor(bitLength / 4294967296), true);

    const state = new Int32Array(this.state);
    processBlocks(state, padded, 0, padded.length);

    const digest = new Uint8Array(16);
    const out = new DataView(digest.buffer);
    out.setInt32(0, state[0], true);
    out.setInt32(4, state[1], true);
    out.setInt32(8, state[2], true);
    out.setInt32(12, state[3], true);
    return digest;
  }

  digestHex(): string {
    return Array.from(this.digest())
      .map((byte) => byte.toString(16).padStart(2, '0'))
      .join('');
  }
}

/**
 * 计算 MD5，返回 16 字节摘要。
 * @param {Uint8Array} input
 * @returns {Uint8Array}
 */
export function md5Bytes(input: Uint8Array): Uint8Array {
  const context = new Md5Context();
  context.update(input);
  return context.digest();
}

/**
 * MD5 十六进制小写串。
 * @param {Uint8Array} input
 * @returns {string}
 */
export function md5Hex(input: Uint8Array): string {
  return Array.from(md5Bytes(input))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

/**
 * MD5 的 base64 形式 —— 正是 MinIO/S3 的 Content-MD5 头需要的值。
 * @param {Uint8Array} input
 * @returns {string}
 */
export function md5Base64(input: Uint8Array): string {
  return bytesToBase64(md5Bytes(input));
}

/**
 * 把 ArrayBuffer / Uint8Array 统一成 Uint8Array。
 * @param {ArrayBuffer | Uint8Array} input
 * @returns {Uint8Array}
 */
export function toUint8Array(input: ArrayBuffer | Uint8Array): Uint8Array {
  return input instanceof Uint8Array ? input : new Uint8Array(input);
}

