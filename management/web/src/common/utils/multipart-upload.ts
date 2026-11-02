/**
 * 知识库文件分片直传（浏览器 -> MinIO）。
 *
 * 流程：
 *   1. POST /init                 拿到 uploadId / chunkSize / totalParts
 *   2. GET  /list-missing-chunk   拿缺失分片（新任务等于全部分片，续传时只补缺的）
 *   3. 对每个缺失分片：
 *        a. Web Worker 算 chunkMd5 = base64(MD5(chunk))
 *        b. POST /get-presigned-part 拿绑定该 MD5 的预签名 PUT URL
 *        c. PUT MinIO，Header 必须带 Content-MD5（已进签名，改了就 403）
 *   4. POST /merge                完整性校验 + 合并 + 写知识库文档
 *
 * 上传前按文件流式算整文件 MD5，`/init` 命中时直接秒传；
 * uploadId 按 kbId + 文件名 + 大小 + lastModified 持久化，刷新页面后可续传。
 */

import { getToken } from '@@/utils/cache/cookies';
import { Md5Context, md5Base64 } from './md5';

const Authorization = 'Authorization';
const api = {
  uploadInit: '/v1/file_upload/init',
  uploadPresignedPart: '/v1/file_upload/get-presigned-part',
  uploadMissingChunk: '/v1/file_upload/list-missing-chunk',
  uploadMerge: '/v1/file_upload/merge',
};

function getAuthorization() {
  const token = getToken();
  return token ? `Bearer ${token}` : '';
}

const DEFAULT_CHUNK_SIZE = 5 * 1024 * 1024;
const FILE_HASH_CHUNK_SIZE = 8 * 1024 * 1024;
const DEFAULT_CONCURRENCY = 5;
const DEFAULT_MAX_RETRY = 3;
const RESUME_STORAGE_PREFIX = 'cvte-rag-plus:multipart-upload:v1';

export type UploadPhase =
  | 'hashing'
  | 'init'
  | 'resume'
  | 'uploading'
  | 'merging'
  | 'done';

export interface UploadProgress {
  phase: UploadPhase;
  uploadId?: string;
  fileName?: string;
  totalParts: number;
  uploadedParts: number;
  percent: number;
}

export interface MultipartUploadOptions {
  /** 知识库 id */
  kbId: string;
  file: File;
  /** 默认 5MB */
  chunkSize?: number;
  /** 默认 5（浏览器同域并发上限约 6） */
  concurrency?: number;
  /** 单分片 PUT 重试次数，默认 3 */
  maxRetry?: number;
  /** 断点续传：传上次的 uploadId 则跳过 init */
  uploadId?: string;
  /** 已计算好的整文件 MD5，不传则内部流式计算 */
  fileMd5?: string;
  signal?: AbortSignal;
  onProgress?: (progress: UploadProgress) => void;
}

export interface MultipartUploadResult {
  uploadId: string;
  docId: string;
  name: string;
  location: string;
  size: number;
  etag: string;
  instant?: boolean;
}

interface UploadTiming {
  hashMs: number;
  initMs: number;
  missingMs: number;
  chunkHashMs: number;
  uploadMs: number;
  mergeMs: number;
  totalParts: number;
  uploadedBefore: number;
  instant: boolean;
  resumed: boolean;
  reinitialized: boolean;
  uploadId?: string;
  chunkSize?: number;
  concurrency: number;
}

interface InitResult {
  uploadId: string;
  chunkSize: number;
  totalParts: number;
  status?: string;
  name?: string;
  location?: string;
  size?: number;
  etag?: string;
  docId?: string;
  instant?: boolean;
}

interface MissingResult {
  uploadId: string;
  status: string;
  name: string;
  size: number;
  chunkSize: number;
  totalParts: number;
  uploadedParts: number;
  missingParts: number[];
}

interface PresignedPart {
  partNumber: number;
  uploaded: boolean;
  url: string | null;
  method: string;
  headers: Record<string, string>;
  expiresIn: number;
}

export class UploadError extends Error {
  code?: number;

  data?: unknown;

  constructor(message: string, code?: number, data?: unknown) {
    super(message);
    this.name = 'UploadError';
    this.code = code;
    this.data = data;
  }
}

function nowMs() {
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

function toSeconds(ms: number) {
  return ms / 1000;
}

function logUploadTiming(
  file: File,
  timing: UploadTiming,
  startedAt: number,
  status: 'success' | 'failed',
  failure?: unknown,
) {
  const totalMs = nowMs() - startedAt;
  const totalSec = toSeconds(totalMs);
  const sizeMB = file.size / 1048576;
  const throughput = totalSec > 0 ? sizeMB / totalSec : 0;
  const mode = timing.instant
    ? 'instant'
    : timing.reinitialized
      ? 'reinit'
      : timing.resumed
        ? 'resume'
        : 'new';
  const chunkSizeMB = timing.chunkSize ? timing.chunkSize / 1048576 : 0;

  // eslint-disable-next-line no-console
  console.info(
    `[前台分片上传埋点] 文件=${file.name} 大小=${sizeMB.toFixed(1)}MB ` +
      `分片=${timing.totalParts}片(${chunkSizeMB ? chunkSizeMB.toFixed(1) : '-'}MB/片) ` +
      `并发=${timing.concurrency} 模式=${mode} 状态=${status} ` +
      `总耗时=${totalSec.toFixed(2)}s 吞吐=${throughput.toFixed(2)}MB/s ` +
      `哈希=${toSeconds(timing.hashMs).toFixed(2)}s ` +
      `初始化=${toSeconds(timing.initMs).toFixed(2)}s ` +
      `查缺=${toSeconds(timing.missingMs).toFixed(2)}s ` +
      `分片=${toSeconds(timing.uploadMs).toFixed(2)}s ` +
      `合并=${toSeconds(timing.mergeMs).toFixed(2)}s`,
  );
  // eslint-disable-next-line no-console
  console.info('[前台分片上传埋点明细]', {
    fileName: file.name,
    sizeMB: Number(sizeMB.toFixed(2)),
    totalParts: timing.totalParts,
    uploadedBefore: timing.uploadedBefore,
    chunkSizeMB: chunkSizeMB ? Number(chunkSizeMB.toFixed(2)) : undefined,
    concurrency: timing.concurrency,
    mode,
    status,
    totalSec: Number(totalSec.toFixed(3)),
    throughputMBps: Number(throughput.toFixed(2)),
    hashSec: Number(toSeconds(timing.hashMs).toFixed(3)),
    initSec: Number(toSeconds(timing.initMs).toFixed(3)),
    missingSec: Number(toSeconds(timing.missingMs).toFixed(3)),
    chunkHashSec: Number(toSeconds(timing.chunkHashMs).toFixed(3)),
    partsSec: Number(toSeconds(timing.uploadMs).toFixed(3)),
    mergeSec: Number(toSeconds(timing.mergeMs).toFixed(3)),
    uploadId: timing.uploadId,
    error:
      failure instanceof Error
        ? failure.message
        : failure
          ? String(failure)
          : undefined,
  });
}

// --------------------------------------------------------------------- http
async function requestJson<T>(url: string, init: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      [Authorization]: getAuthorization(),
      ...(init.headers as Record<string, string>),
    },
  });
  let payload: any = null;
  try {
    payload = await response.json();
  } catch (e) {
    throw new UploadError(`请求失败：HTTP ${response.status}`);
  }
  if (!payload || payload.code !== 0) {
    throw new UploadError(
      payload?.message || `请求失败：HTTP ${response.status}`,
      payload?.code,
      payload?.data,
    );
  }
  return payload.data as T;
}

function postJson<T>(url: string, body: unknown, signal?: AbortSignal) {
  return requestJson<T>(url, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
    signal,
  });
}

function getJson<T>(
  url: string,
  params: Record<string, string>,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams(params).toString();
  return requestJson<T>(`${url}?${query}`, { method: 'GET', signal });
}

// ----------------------------------------------------------------- md5 pool
interface Md5Pool {
  digest(blob: Blob): Promise<string>;
  destroy(): void;
}

let md5TaskId = 0;

function digestWithWorker(worker: Worker, blob: Blob): Promise<string> {
  return blob.arrayBuffer().then(
    (buffer) =>
      new Promise<string>((resolve, reject) => {
        const id = (md5TaskId += 1);
        const onMessage = (event: MessageEvent) => {
          if (!event.data || event.data.id !== id) {
            return;
          }
          worker.removeEventListener('message', onMessage);
          worker.removeEventListener('error', onError);
          if (event.data.error) {
            reject(new UploadError(`MD5 计算失败：${event.data.error}`));
          } else {
            resolve(event.data.md5 as string);
          }
        };
        const onError = (event: ErrorEvent) => {
          worker.removeEventListener('message', onMessage);
          worker.removeEventListener('error', onError);
          reject(new UploadError(`MD5 Worker 异常：${event.message}`));
        };
        worker.addEventListener('message', onMessage);
        worker.addEventListener('error', onError);
        worker.postMessage({ id, buffer }, [buffer]);
      }),
  );
}

/**
 * Worker 池：每个 Worker 串行处理，多个 Worker 之间并行。
 * Worker 创建失败（老浏览器 / 打包环境不支持）时自动退化为主线程计算。
 */
function createMd5Pool(size: number): Md5Pool {
  const workers: Worker[] = [];
  try {
    for (let i = 0; i < size; i += 1) {
      workers.push(
        new Worker(new URL('./md5.worker.ts', import.meta.url), {
          type: 'module',
        }),
      );
    }
  } catch (e) {
    workers.forEach((worker) => worker.terminate());
    workers.length = 0;
  }

  if (!workers.length) {
    return {
      digest: (blob) =>
        blob.arrayBuffer().then((buffer) => md5Base64(new Uint8Array(buffer))),
      destroy: () => undefined,
    };
  }

  const lanes = workers.map((worker) => {
    let chain: Promise<unknown> = Promise.resolve();
    return (blob: Blob) => {
      const run = chain.then(() => digestWithWorker(worker, blob));
      chain = run.catch(() => undefined);
      return run;
    };
  });

  let cursor = 0;
  return {
    digest(blob: Blob) {
      const lane = lanes[cursor % lanes.length];
      cursor += 1;
      return lane(blob);
    },
    destroy() {
      workers.forEach((worker) => worker.terminate());
    },
  };
}

// --------------------------------------------------------------- file hash
function resumeStorageKey(kbId: string, file: File) {
  return [
    RESUME_STORAGE_PREFIX,
    encodeURIComponent(kbId),
    encodeURIComponent(file.name),
    file.size,
    file.lastModified,
  ].join(':');
}

function loadResumeUploadId(kbId: string, file: File): string | undefined {
  try {
    return (
      globalThis.localStorage?.getItem(resumeStorageKey(kbId, file)) ||
      undefined
    );
  } catch (error) {
    return undefined;
  }
}

function saveResumeUploadId(kbId: string, file: File, uploadId: string) {
  try {
    globalThis.localStorage?.setItem(resumeStorageKey(kbId, file), uploadId);
  } catch (error) {
    // localStorage 不可用时只是不能跨刷新续传，不影响当前上传
  }
}

function clearResumeUploadId(kbId: string, file: File) {
  try {
    globalThis.localStorage?.removeItem(resumeStorageKey(kbId, file));
  } catch (error) {
    // ignore
  }
}

function hashFileWithWorker(
  file: File,
  onProgress: (percent: number) => void,
  signal?: AbortSignal,
): Promise<string> {
  const worker = new Worker(new URL('./md5.worker.ts', import.meta.url), {
    type: 'module',
  });

  return new Promise<string>((resolve, reject) => {
    const id = (md5TaskId += 1);

    const cleanup = () => {
      worker.terminate();
      signal?.removeEventListener('abort', onAbort);
    };
    const onAbort = () => {
      cleanup();
      reject(new UploadError('上传已取消'));
    };
    const onMessage = (event: MessageEvent) => {
      if (!event.data || event.data.id !== id) {
        return;
      }
      if (typeof event.data.progress === 'number') {
        onProgress(event.data.progress);
        return;
      }
      cleanup();
      if (event.data.error) {
        reject(new UploadError(`MD5 计算失败：${event.data.error}`));
      } else {
        resolve(event.data.fileMd5 as string);
      }
    };
    const onError = (event: ErrorEvent) => {
      cleanup();
      reject(new UploadError(`MD5 Worker 异常：${event.message}`));
    };

    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener('abort', onAbort, { once: true });
    worker.addEventListener('message', onMessage);
    worker.addEventListener('error', onError);
    worker.postMessage({
      id,
      type: 'file',
      file,
      chunkSize: FILE_HASH_CHUNK_SIZE,
    });
  });
}

async function hashFileOnMainThread(
  file: File,
  onProgress: (percent: number) => void,
  signal?: AbortSignal,
): Promise<string> {
  const context = new Md5Context();
  let offset = 0;
  while (offset < file.size) {
    throwIfAborted(signal);
    // eslint-disable-next-line no-await-in-loop
    const buffer = await file
      .slice(offset, Math.min(offset + FILE_HASH_CHUNK_SIZE, file.size))
      .arrayBuffer();
    context.update(new Uint8Array(buffer));
    offset += buffer.byteLength;
    onProgress(file.size ? Math.round((offset / file.size) * 100) : 100);
    // 让出主线程，避免大文件计算期间页面完全无响应
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return context.digestHex();
}

async function hashFile(
  file: File,
  onProgress: (percent: number) => void,
  signal?: AbortSignal,
): Promise<string> {
  try {
    return await hashFileWithWorker(file, onProgress, signal);
  } catch (error) {
    if (signal?.aborted) {
      throw error;
    }
    // Worker 创建/运行失败时退化到主线程，功能不受影响
    return hashFileOnMainThread(file, onProgress, signal);
  }
}

// -------------------------------------------------------------------- utils
function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function runWithConcurrency<T>(
  items: T[],
  limit: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  let index = 0;
  const runners = Array.from(
    { length: Math.min(limit, items.length) },
    async () => {
      while (index < items.length) {
        const current = items[index];
        index += 1;
        // eslint-disable-next-line no-await-in-loop
        await worker(current);
      }
    },
  );
  await Promise.all(runners);
}

function throwIfAborted(signal?: AbortSignal) {
  if (signal?.aborted) {
    throw new UploadError('上传已取消');
  }
}

// --------------------------------------------------------------------- main
async function multipartUploadImpl(
  options: MultipartUploadOptions,
  timing: UploadTiming,
): Promise<MultipartUploadResult> {
  const {
    kbId,
    file,
    concurrency = DEFAULT_CONCURRENCY,
    maxRetry = DEFAULT_MAX_RETRY,
    signal,
    onProgress,
  } = options;
  let chunkSize = options.chunkSize || DEFAULT_CHUNK_SIZE;
  let resumedFromStorage = false;
  let uploadId = options.uploadId;
  if (!uploadId) {
    uploadId = loadResumeUploadId(kbId, file);
    resumedFromStorage = Boolean(uploadId);
  }
  timing.resumed = resumedFromStorage;
  timing.uploadId = uploadId;

  const emit = (
    progress: Partial<UploadProgress> & Pick<UploadProgress, 'phase'>,
  ) => {
    onProgress?.({
      fileName: file.name,
      totalParts: 0,
      uploadedParts: 0,
      percent: 0,
      ...progress,
    } as UploadProgress);
  };

  const initUpload = async (): Promise<MultipartUploadResult | undefined> => {
    let fileMd5 = options.fileMd5;
    if (!fileMd5) {
      // 整文件指纹只读一次文件；分片上传时仍按 5MB 独立计算 Content-MD5
      const hashStartedAt = nowMs();
      fileMd5 = await hashFile(
        file,
        (percent) => emit({ phase: 'hashing', percent }),
        signal,
      );
      timing.hashMs += nowMs() - hashStartedAt;
    }

    emit({ phase: 'init' });
    const initStartedAt = nowMs();
    let init: InitResult;
    try {
      init = await postJson<InitResult>(
        api.uploadInit,
        {
          kb_id: kbId,
          name: file.name,
          size: file.size,
          chunk_size: chunkSize,
          fileMd5,
        },
        signal,
      );
    } finally {
      timing.initMs += nowMs() - initStartedAt;
    }
    uploadId = init.uploadId;
    timing.uploadId = uploadId;
    chunkSize = init.chunkSize || chunkSize;
    saveResumeUploadId(kbId, file, uploadId);

    if (init.instant) {
      timing.instant = true;
      if (!init.docId) {
        throw new UploadError('秒传成功但响应缺少 docId');
      }
      clearResumeUploadId(kbId, file);
      emit({
        phase: 'done',
        uploadId,
        totalParts: 0,
        uploadedParts: 0,
        percent: 100,
      });
      return {
        uploadId,
        docId: init.docId,
        name: init.name || file.name,
        location: init.location || file.name,
        size: init.size ?? file.size,
        etag: init.etag || '',
        instant: true,
      };
    }
    return undefined;
  };

  // 1) 初始化；若上次 uploadId 仍保存在本地则先尝试续传
  if (!uploadId) {
    const instantResult = await initUpload();
    if (instantResult) {
      return instantResult;
    }
  }
  if (!uploadId) {
    throw new UploadError('初始化上传任务失败');
  }

  // 2) 断点续传：只传缺失分片
  emit({ phase: 'resume', uploadId });
  const fetchMissing = () =>
    getJson<MissingResult>(
      api.uploadMissingChunk,
      { uploadId: uploadId as string },
      signal,
    );
  const timedFetchMissing = async () => {
    const startedAt = nowMs();
    try {
      return await fetchMissing();
    } finally {
      timing.missingMs += nowMs() - startedAt;
    }
  };

  let state: MissingResult;
  try {
    state = await timedFetchMissing();
  } catch (error) {
    const code = error instanceof UploadError ? error.code : undefined;
    if (resumedFromStorage && (code === 404 || code === 101)) {
      // 任务过期、被清理或换了登录用户：清掉本地记录，重新 init
      clearResumeUploadId(kbId, file);
      uploadId = undefined;
      resumedFromStorage = false;
      timing.resumed = false;
      timing.reinitialized = true;
      const instantResult = await initUpload();
      if (instantResult) {
        return instantResult;
      }
      state = await timedFetchMissing();
    } else {
      throw error;
    }
  }

  if (state.size && state.size !== file.size) {
    if (resumedFromStorage && !options.uploadId) {
      clearResumeUploadId(kbId, file);
      uploadId = undefined;
      resumedFromStorage = false;
      timing.resumed = false;
      timing.reinitialized = true;
      const instantResult = await initUpload();
      if (instantResult) {
        return instantResult;
      }
      state = await timedFetchMissing();
    }
  }
  if (state.size && state.size !== file.size) {
    // 续传时换了文件，继续传只会拼出一个坏文件
    throw new UploadError(
      `续传文件与任务不一致：任务 ${state.size} 字节，当前文件 ${file.size} 字节`,
    );
  }
  chunkSize = state.chunkSize || chunkSize;
  const activeUploadId = uploadId;
  if (!activeUploadId) {
    throw new UploadError('初始化上传任务失败');
  }
  const { totalParts } = state;
  let uploadedParts = totalParts - state.missingParts.length;
  timing.totalParts = totalParts;
  timing.uploadedBefore = uploadedParts;
  timing.chunkSize = chunkSize;
  timing.uploadId = activeUploadId;
  const report = (phase: UploadPhase = 'uploading') => {
    emit({
      phase,
      uploadId: activeUploadId,
      totalParts,
      uploadedParts,
      percent: totalParts ? Math.round((uploadedParts / totalParts) * 100) : 0,
    });
  };
  report();

  const pool = createMd5Pool(concurrency);

  const uploadOnePart = async (partNumber: number) => {
    throwIfAborted(signal);
    const start = (partNumber - 1) * chunkSize;
    const blob = file.slice(start, Math.min(start + chunkSize, file.size));
    const chunkHashStartedAt = nowMs();
    let chunkMd5: string;
    try {
      chunkMd5 = await pool.digest(blob);
    } finally {
      timing.chunkHashMs += nowMs() - chunkHashStartedAt;
    }
    throwIfAborted(signal);

    let lastError: unknown = null;
    for (let attempt = 0; attempt <= maxRetry; attempt += 1) {
      try {
        // 每次重试都重新签名：URL 有有效期，403 多半是签名/过期问题
        const signed = await postJson<PresignedPart>(
          api.uploadPresignedPart,
          { uploadId: activeUploadId, partNumber, chunkMd5 },
          signal,
        );
        if (signed.uploaded || !signed.url) {
          return;
        }

        const putResponse = await fetch(signed.url, {
          method: 'PUT',
          headers: signed.headers,
          body: blob,
          signal,
        });
        if (!putResponse.ok) {
          const detail = await putResponse.text().catch(() => '');
          throw new UploadError(
            `分片 ${partNumber} 上传失败：HTTP ${putResponse.status} ${detail.slice(0, 200)}`,
          );
        }

        uploadedParts += 1;
        report();
        return;
      } catch (error) {
        lastError = error;
        if (signal?.aborted) {
          throw error;
        }
        if (attempt < maxRetry) {
          // eslint-disable-next-line no-await-in-loop
          await sleep(300 * 2 ** attempt);
        }
      }
    }
    throw lastError instanceof Error
      ? lastError
      : new UploadError(`分片 ${partNumber} 上传失败`);
  };

  try {
    const partsStartedAt = nowMs();
    try {
      await runWithConcurrency(state.missingParts, concurrency, uploadOnePart);
    } finally {
      timing.uploadMs += nowMs() - partsStartedAt;
    }

    // 3) 合并；后端若发现还有缺失（极端情况下列表不一致），补传后再合一次
    for (let round = 0; round < 2; round += 1) {
      report('merging');
      const mergeStartedAt = nowMs();
      let merged: any;
      try {
        try {
          merged = await postJson<any>(
            api.uploadMerge,
            { uploadId: activeUploadId },
            signal,
          );
        } finally {
          timing.mergeMs += nowMs() - mergeStartedAt;
        }
        clearResumeUploadId(kbId, file);
        emit({
          phase: 'done',
          uploadId: activeUploadId,
          totalParts,
          uploadedParts,
          percent: 100,
        });
        return {
          uploadId: activeUploadId,
          docId: merged.docId || '',
          name: merged.name || state.name || file.name,
          location: merged.location || state.name || file.name,
          size: merged.size ?? state.size ?? file.size,
          etag: merged.etag || '',
        };
      } catch (error) {
        const detail = (error as UploadError)?.data as
          | { missingParts?: number[] }
          | undefined;
        const stillMissing = detail?.missingParts || [];
        if (round === 1 || !stillMissing.length) {
          throw error;
        }
        // eslint-disable-next-line no-await-in-loop
        const retryPartsStartedAt = nowMs();
        try {
          await runWithConcurrency(stillMissing, concurrency, uploadOnePart);
        } finally {
          timing.uploadMs += nowMs() - retryPartsStartedAt;
        }
      }
    }
    throw new UploadError('合并失败');
  } finally {
    pool.destroy();
  }
}

export async function multipartUpload(
  options: MultipartUploadOptions,
): Promise<MultipartUploadResult> {
  const timing: UploadTiming = {
    hashMs: 0,
    initMs: 0,
    missingMs: 0,
    chunkHashMs: 0,
    uploadMs: 0,
    mergeMs: 0,
    totalParts: 0,
    uploadedBefore: 0,
    instant: false,
    resumed: false,
    reinitialized: false,
    concurrency: options.concurrency ?? DEFAULT_CONCURRENCY,
  };
  const startedAt = nowMs();
  let failure: unknown;
  try {
    return await multipartUploadImpl(options, timing);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    logUploadTiming(
      options.file,
      timing,
      startedAt,
      failure ? 'failed' : 'success',
      failure,
    );
  }
}

export default multipartUpload;

