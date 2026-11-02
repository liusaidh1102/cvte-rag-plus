<!-- eslint-disable vue/custom-event-name-casing -->
<script>
import { getDocumentParseProgress } from "@@/apis/kbs/document"
import { ElMessage } from "element-plus"

export default {
  name: "DocumentParseProgress",
  props: {
    documentId: {
      type: String,
      required: true
    },
    visible: {
      type: Boolean,
      default: false
    }
  },
  emits: ["close", "parse-complete", "parse-failed"],
  data() {
    return {
      dialogVisible: false,
      progress: 0,
      progressMessage: "正在准备解析...",
      logs: [],
      pollingTimer: null,
      isCompleted: false,
      hasError: false,
      progressStatus: "",
      // 连续请求失败计数：解析期间后端偶发变慢属正常，累计到阈值才提示
      consecutiveFailures: 0,
      // 防重入：保证同一时刻只有一个进度请求在途
      inFlight: false,
      // 组件已销毁标志，避免定时器回调在卸载后继续触发
      isUnmounted: false
    }
  },
  computed: {
    progressPercentage() {
      return Math.round(this.progress * 100)
    }
  },
  watch: {
    visible(newVal) {
      this.dialogVisible = newVal
      if (newVal) {
        this.startPolling()
      } else {
        this.stopPolling()
      }
    },
    dialogVisible(newVal) {
      if (!newVal) {
        this.$emit("close")
      }
    },
    documentId() {
      this.resetProgress()
      if (this.dialogVisible) {
        this.startPolling()
      }
    }
  },
  beforeUnmount() {
    this.isUnmounted = true
    this.stopPolling()
    this.dialogVisible = false
  },
  methods: {
    resetProgress() {
      this.progress = 0
      this.progressMessage = "正在准备解析..."
      this.logs = []
      this.isCompleted = false
      this.hasError = false
      this.progressStatus = ""
      this.consecutiveFailures = 0
      this.inFlight = false
    },

    startPolling() {
      this.resetProgress()
      this.scheduleNextPoll(0)
    },

    /**
     * 自调度轮询：上一次请求结束后才安排下一次。
     * 相比 setInterval，可避免后端变慢时请求堆积（堆积会进一步加剧后端压力）。
     */
    scheduleNextPoll(delay) {
      this.stopPolling()
      if (this.isUnmounted) return
      this.pollingTimer = setTimeout(() => {
        this.pollingTimer = null
        this.fetchProgress()
      }, delay)
    },

    stopPolling() {
      if (this.pollingTimer) {
        clearTimeout(this.pollingTimer)
        this.pollingTimer = null
      }
    },

    async fetchProgress() {
      // 防重入：若上一次请求尚未返回，跳过本次，避免并发堆积
      if (this.inFlight) {
        this.scheduleNextPoll(3000)
        return
      }
      this.inFlight = true
      let shouldContinue = true
      try {
        const response = await getDocumentParseProgress(this.documentId)

        // 请求成功，重置失败计数
        this.consecutiveFailures = 0

        if (response?.code === 202) {
          this.progressMessage = "解析进行中，请稍候..."
        } else if (response.code === 0) {
          const data = response.data

          // 更新进度
          this.progress = data.progress || 0

          // 更新消息
          if (data.message && data.message !== this.progressMessage) {
            this.progressMessage = data.message

            // 检查是否已存在相同的日志消息，避免重复
            const isDuplicate = this.logs.some(log => log.message === data.message)

            if (!isDuplicate) {
              // 添加到日志（使用 push 而不是 unshift，保持时间顺序）
              const now = new Date()
              const timeStr = `${now.getHours().toString().padStart(2, "0")}:${now.getMinutes().toString().padStart(2, "0")}:${now.getSeconds().toString().padStart(2, "0")}`

              this.logs.push({
                time: timeStr,
                message: data.message
              })

              // 限制日志数量，移除最早的日志
              if (this.logs.length > 20) {
                this.logs.shift()
              }
            }
          }

          // 检查是否完成
          if (data.running === "3") {
            this.isCompleted = true
            this.progressStatus = "success"
            shouldContinue = false
            this.$emit("parse-complete")
          }

          // 检查是否出错（后端明确返回解析失败，与前端请求超时区分开）
          if (data.status === "3") {
            this.hasError = true
            this.progressStatus = "exception"
            shouldContinue = false
            this.$emit("parse-failed", data.message || "解析失败")
          }
        }
      } catch (error) {
        // 请求失败不等于解析失败：解析期间后端 CPU 吃紧，轮询请求可能偶发超时，
        // 这类情况只记录、继续轮询，累计到阈值才提示用户。
        this.consecutiveFailures++
        console.warn(`获取解析进度失败（连续第 ${this.consecutiveFailures} 次）:`, error?.message || error)

        if (this.consecutiveFailures >= 3) {
          // 仅在仍未完成时提示，避免与完成状态冲突
          if (!this.isCompleted && !this.hasError) {
            this.progressMessage = "进度查询超时，正在重试…（解析仍在后台进行）"
          }
        }
      } finally {
        this.inFlight = false
        if (shouldContinue && !this.isCompleted && !this.hasError) {
          this.scheduleNextPoll(3000)
        } else {
          this.stopPolling()
        }
      }
    },

    handleClose() {
      this.stopPolling()
      this.dialogVisible = false
    },

    // 获取简化的进度消息
    getSimplifiedMessage() {
      if (this.isCompleted) {
        return "解析完成"
      }
      if (this.hasError) {
        return "解析失败"
      }
      if (this.progress > 0 && this.progress < 1) {
        return "解析中"
      }
      return this.progressMessage || "正在准备解析..."
    }
  }
}
</script>

<template>
  <div class="document-parse-progress-wrapper">
    <el-dialog v-model="dialogVisible" title="文档解析进度" width="500px">
      <div class="progress-container">
        <el-progress
          :percentage="progressPercentage"
          :status="progressStatus"
        />
        <div class="progress-message">
          {{ getSimplifiedMessage() }}
        </div>

        <div class="progress-logs" ref="logsContainer">
          <div v-for="(log, index) in logs" :key="index" class="log-item">
            <span class="log-time">{{ log.time }}</span>
            <span class="log-message">{{ log.message }}</span>
          </div>
        </div>
      </div>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="handleClose" :disabled="!isCompleted && !hasError">取消</el-button>
          <el-button type="primary" @click="handleClose" v-if="isCompleted || hasError">确定</el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.progress-container {
  padding: 10px;
}

.progress-message {
  margin-top: 10px;
  font-size: 14px;
  text-align: center;
}


.progress-logs {
  margin-top: 20px;
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 10px;
}

.log-item {
  margin-bottom: 5px;
  font-size: 12px;
  line-height: 1.4;
  padding: 2px 0;
  border-bottom: 1px solid #f0f0f0;
}

.log-item:last-child {
  border-bottom: none;
}

.log-time {
  color: #909399;
  margin-right: 10px;
  font-weight: 500;
}

.log-message {
  color: #606266;
  word-break: break-all;
}
</style>
