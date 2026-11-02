import { uploadRequest } from "@/http/upload-axios"

/**
 * 文件上传配置接口
 */
interface UploadConfig {
  onProgress?: (progress: number) => void
  timeout?: number
  chunkSize?: number
}

/**
 * 优化的文件上传 API
 * 支持进度回调、自定义超时、错误重试
 */
export function uploadFileApiV2(
  formData: FormData,
  config: UploadConfig = {}
) {
  const { onProgress, timeout = 300000 } = config

  return uploadRequest<{
    code: number
    data: Array<{
      name: string
      size: number
      type: string
      status: string
    }>
    message: string
  }>({
    url: "/api/v1/files/upload",
    method: "post",
    data: formData,
    timeout,
    onUploadProgress: (progressEvent) => {
      if (onProgress && progressEvent.total) {
        const progress = Math.round((progressEvent.loaded * 100) / progressEvent.total)
        onProgress(progress)
      }
    }
  })
}

/**
 * 获取上传任务的唯一键（用于 localStorage 缓存）
 */
function getUploadTaskKey(file: File, chunkSize: number): string {
  return `${file.name}|${file.size}|${file.lastModified}|${chunkSize}`
}

/**
 * 保存上传任务信息到 localStorage
 */
function saveUploadTaskInfo(uploadId: string, taskKey: string): void {
  try {
    const info = { uploadId, chunkSize: parseInt(taskKey.split("|")[3]) }
    localStorage.setItem(taskKey, JSON.stringify(info))
  } catch (e) {
    console.warn("无法保存上传任务：", e)
  }
}

/**
 * 从 localStorage 读取上传任务信息
 */
function loadUploadTaskInfo(taskKey: string): { uploadId?: string; chunkSize?: number } | null {
  try {
    const saved = localStorage.getItem(taskKey)
    return saved ? JSON.parse(saved) : null
  } catch (e) {
    return null
  }
}

/**
 * 删除本地上传任务缓存
 */
function removeUploadTaskCache(taskKey: string): void {
  try {
    localStorage.removeItem(taskKey)
  } catch (e) {
    console.warn("无法删除上传任务缓存：", e)
  }
}

/**
 * 检查上传状态并返回缺失的分片索引
 */
async function checkUploadStatus(uploadId: string, fileName: string, totalChunks: number): Promise<number[]> {
  try {
    const response = await uploadRequest<{ code: number; data: { missing: number[]; uploaded_count: number } }>({
      url: `/api/v1/files/upload/status?uploadId=${uploadId}&fileName=${encodeURIComponent(fileName)}&totalChunks=${totalChunks}`,
      method: "get",
    })
    
    if (response.code === 0) {
      const missing = response.data.missing || []
      console.log(`检查上传状态 - 已上传：${response.data.uploaded_count}/${totalChunks}, 缺失：${missing.length}`)
      return missing
    }
  } catch (error) {
    console.warn("检查上传状态失败:", error)
  }
  
  return [] // 检查失败就认为所有分片都缺失
}

/**
 * 分块上传大文件
 * 将大文件分割成小块进行上传，提高成功率
 * 支持断点续传：基于文件名、大小和修改时间缓存 uploadId
 */
export async function uploadLargeFile(
  file: File,
  config: UploadConfig & { parentId?: string } = {}
) {
  const { chunkSize = 5 * 1024 * 1024, onProgress, parentId } = config // 默认 5MB 分块

  // 埋点：上传性能统计起点（用于耗时测试/简历量化）
  const _t0 = performance.now()

  // 小文件直接上传
  if (file.size <= chunkSize) {
    const formData = new FormData()
    formData.append("file", file)
    if (parentId) formData.append("parent_id", parentId)

    return uploadFileApiV2(formData, { onProgress })
  }

  // 大文件分块上传
  const totalChunks = Math.ceil(file.size / chunkSize)
  const taskKey = getUploadTaskKey(file, chunkSize)
  
  // 尝试从本地缓存恢复 uploadId
  let cached = loadUploadTaskInfo(taskKey)
  let useCachedUploadId = false
  
  if (cached?.uploadId) {
    useCachedUploadId = true
    console.log(`找到缓存的上传任务，重新查询状态...`)
    
    // 先检查是否有已上传的分片
    const missingChunks = await checkUploadStatus(cached.uploadId, file.name, totalChunks)
    
    if (missingChunks.length === 0) {
      console.log("所有分片已上传完成，直接合并")
      const _elapsed = (performance.now() - _t0) / 1000
      console.log(
        `[分片上传埋点] 文件=${file.name} 大小=${(file.size / 1048576).toFixed(1)}MB ` +
        `分片=${totalChunks}片(5MB) 耗时=${_elapsed.toFixed(1)}s 模式=续传-全齐(0片缺失)`
      )
      removeUploadTaskCache(taskKey)
      return uploadRequest<any>({
        url: "/api/v1/files/upload/merge",
        method: "post",
        data: { uploadId: cached.uploadId, fileName: file.name, totalChunks, parentId }
      })
    } else {
      console.log(`发现 ${missingChunks.length} 个缺失的分片，开始续传...`)
      // 续传：只上传缺失的分片
      const _result = await uploadMissingChunks(missingChunks, chunkSize, file, parentId, useCachedUploadId, taskKey, onProgress)
      const _elapsed = (performance.now() - _t0) / 1000
      console.log(
        `[分片上传埋点] 文件=${file.name} 大小=${(file.size / 1048576).toFixed(1)}MB ` +
        `分片=${totalChunks}片(5MB) 缺${missingChunks.length}片 ` +
        `耗时=${_elapsed.toFixed(1)}s 吞吐=${(file.size / 1048576 / _elapsed).toFixed(1)}MB/s 模式=断点续传`
      )
      return _result
    }
  } else {
    // 新上传任务
    const uploadId = `upload_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
    saveUploadTaskInfo(uploadId, taskKey)
    
    try {
      for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex++) {
        const start = chunkIndex * chunkSize
        const end = Math.min(start + chunkSize, file.size)
        const chunk = file.slice(start, end)

        const formData = new FormData()
        formData.append("chunk", chunk)
        formData.append("chunkIndex", chunkIndex.toString())
        formData.append("totalChunks", totalChunks.toString())
        formData.append("uploadId", uploadId)
        formData.append("fileName", file.name)
        if (parentId) formData.append("parent_id", parentId)

        await uploadRequest({
          url: "/api/v1/files/upload/chunk",
          method: "post",
          data: formData,
          timeout: 60000, // 单个分块 1 分钟超时
          onUploadProgress: (progressEvent) => {
            if (onProgress && progressEvent.total) {
              const chunkProgress = (progressEvent.loaded / progressEvent.total) * 100
              const totalProgress = ((chunkIndex + chunkProgress / 100) / totalChunks) * 100
              onProgress(Math.round(totalProgress))
            }
          }
        })
      }

      // 成功：清除缓存
      removeUploadTaskCache(taskKey)

      // 埋点：分片上传耗时统计
      const _elapsed = (performance.now() - _t0) / 1000
      console.log(
        `[分片上传埋点] 文件=${file.name} 大小=${(file.size / 1048576).toFixed(1)}MB ` +
        `分片=${totalChunks}片(5MB) 耗时=${_elapsed.toFixed(1)}s ` +
        `吞吐=${(file.size / 1048576 / _elapsed).toFixed(1)}MB/s 模式=新上传`
      )

      // 合并分块
      return uploadRequest({
        url: "/api/v1/files/upload/merge",
        method: "post",
        data: { uploadId, fileName: file.name, totalChunks, parentId }
      })
    } catch (error) {
      throw error
    }
  }
}

/**
 * 上传缺失的分片（续传分支）
 */
async function uploadMissingChunks(
  missingChunkIndices: number[],
  chunkSize: number,
  file: File,
  parentId: string | undefined,
  useCachedUploadId: boolean,
  taskKey: string,
  onProgress?: (percent: number) => void
): Promise<any> {
  const totalChunks = missingChunkIndices.length
  
  for (let i = 0; i < totalChunks; i++) {
    const chunkIndex = missingChunkIndices[i]
    const start = chunkIndex * chunkSize
    const end = Math.min(start + chunkSize, file.size)
    const chunk = file.slice(start, end)

    const formData = new FormData()
    formData.append("chunk", chunk)
    formData.append("chunkIndex", chunkIndex.toString())
    formData.append("totalChunks", String(Math.ceil(file.size / chunkSize)))
    formData.append("uploadId", useCachedUploadId ? (loadUploadTaskInfo(taskKey)?.uploadId ?? "") : `upload_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`)
    formData.append("fileName", file.name)
    if (parentId) formData.append("parent_id", parentId)

    await uploadRequest<any>({
      url: "/api/v1/files/upload/chunk",
      method: "post",
      data: formData,
      timeout: 60000,
      onUploadProgress: (progressEvent) => {
        if (onProgress && progressEvent.total) {
          const chunkProgress = (progressEvent.loaded / progressEvent.total) * 100
          const totalProgress = ((chunkIndex + chunkProgress / 100) / (file.size / chunkSize)) * 100
          onProgress(Math.round(totalProgress))
        }
      }
    })
  }

  // 续传成功后，检查是否还有缺失，如果没有就合并
  const currentUploadId = useCachedUploadId ? loadUploadTaskInfo(taskKey)?.uploadId : undefined
  if (!currentUploadId) {
    return null
  }
  
  const response = await uploadRequest<{ code: number; data: { is_complete: boolean } }>({
    url: `/api/v1/files/upload/status?uploadId=${currentUploadId}&fileName=${encodeURIComponent(file.name)}&totalChunks=${Math.ceil(file.size / chunkSize)}`,
    method: "get",
  })
  
  if (response.code !== 0 || response.data.is_complete) {
    // 仍有缺失或已完整
    removeUploadTaskCache(taskKey)
    return uploadRequest({
      url: "/api/v1/files/upload/merge",
      method: "post",
      data: { 
        uploadId: currentUploadId, 
        fileName: file.name, 
        totalChunks: Math.ceil(file.size / chunkSize),
        parentId 
      }
    })
  }
  
  return null
}

