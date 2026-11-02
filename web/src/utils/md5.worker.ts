/// <reference lib="webworker" />

/**
 * 分片 MD5 计算 Worker。
 *
 * 5MB 分片在主线程算 MD5 大约几十毫秒，看着不多，但 5 个并发 + 用户滚动页面时
 * 就会掉帧，所以丢到 Worker 里算。主线程那边维护一个 Worker 池（默认 5 个，
 * 与上传并发一致），每个 Worker 同一时刻只算一片。
 */

import { Md5Context, md5Base64 } from './md5';

interface Md5BufferRequest {
  id: number;
  buffer: ArrayBuffer;
}

interface Md5FileRequest {
  id: number;
  type: 'file';
  file: File;
  chunkSize?: number;
}

type Md5Request = Md5BufferRequest | Md5FileRequest;

const FILE_READ_SIZE = 8 * 1024 * 1024;

const ctx = self as unknown as DedicatedWorkerGlobalScope;

ctx.onmessage = async (event: MessageEvent<Md5Request>) => {
  const { id } = event.data;

  if ('file' in event.data) {
    const { file, chunkSize = FILE_READ_SIZE } = event.data;
    try {
      const context = new Md5Context();
      let offset = 0;
      while (offset < file.size) {
        // eslint-disable-next-line no-await-in-loop
        const buffer = await file
          .slice(offset, Math.min(offset + chunkSize, file.size))
          .arrayBuffer();
        context.update(new Uint8Array(buffer));
        offset += buffer.byteLength;
        ctx.postMessage({
          id,
          progress: file.size ? Math.round((offset / file.size) * 100) : 100,
        });
      }
      ctx.postMessage({ id, fileMd5: context.digestHex(), done: true });
    } catch (error) {
      ctx.postMessage({ id, error: String(error) });
    }
    return;
  }

  const { buffer } = event.data;
  try {
    const md5 = md5Base64(new Uint8Array(buffer));
    ctx.postMessage({ id, md5 });
  } catch (error) {
    ctx.postMessage({ id, error: String(error) });
  }
};
