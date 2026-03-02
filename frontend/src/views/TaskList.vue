<template>
  <div>
    <div class="page-header">
      <h2>📋 任务管理</h2>
      <el-button type="primary" @click="$router.push('/tasks/create')">
        <el-icon><Plus /></el-icon> 创建任务
      </el-button>
    </div>

    <el-table :data="tasks" stripe v-loading="loading" row-key="id">
      <!-- 展开行：视频子任务列表 -->
      <el-table-column type="expand">
        <template #default="{ row }">
          <div style="padding: 10px 20px;">
            <h4 style="margin-bottom: 10px;">📹 视频子任务（共 {{ row.videos ? row.videos.length : row.video_count }} 个）</h4>
            <el-table :data="row.videos || []" size="small" border>
              <el-table-column prop="sequence" label="序号" width="60" />
              <el-table-column prop="file_name" label="文件名" show-overflow-tooltip />
              <el-table-column prop="page_name" label="公共主页" width="150" show-overflow-tooltip>
                <template #default="{ row: v }">
                  <el-tag size="small" effect="plain">{{ v.page_name || '-' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="file_size" label="大小(MB)" width="90">
                <template #default="{ row: v }">{{ v.file_size ? v.file_size.toFixed(2) : '-' }}</template>
              </el-table-column>
              <el-table-column prop="scheduled_time" label="计划时间" width="180">
                <template #default="{ row: v }">
                  {{ v.scheduled_time ? new Date(v.scheduled_time).toLocaleString('zh-CN') : '-' }}
                </template>
              </el-table-column>
              <el-table-column prop="status" label="状态" width="120">
                <template #default="{ row: v }">
                  <el-tag :type="videoStatusType(v.status)" size="small">{{ videoStatusLabel(v.status) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="error_message" label="错误信息" show-overflow-tooltip />
            </el-table>
          </div>
        </template>
      </el-table-column>

      <el-table-column prop="task_name" label="任务名称" width="200" />
      <el-table-column prop="description" label="描述" show-overflow-tooltip />
      <el-table-column prop="start_time" label="起始时间" width="180">
        <template #default="{ row }">
          {{ new Date(row.start_time).toLocaleString('zh-CN') }}
        </template>
      </el-table-column>
      <el-table-column prop="interval_minutes" label="间隔(分钟)" width="100" />
      <el-table-column prop="video_count" label="视频数" width="80" />
      <el-table-column prop="status" label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="taskStatusType(row.status)">{{ taskStatusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="280" fixed="right">
        <template #default="{ row }">
          <el-button size="small" type="success" @click="executeTask(row)"
                     :disabled="row.status === 'running'">执行</el-button>
          <el-button size="small" type="warning" @click="resumeTask(row)"
                     v-if="row.status === 'paused' || row.status === 'waiting_auth'">恢复</el-button>
          <el-button size="small" @click="viewLogs(row)">日志</el-button>
          <el-button size="small" type="danger" @click="deleteTask(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { taskApi, publisherApi } from '../api/index.js'

const router = useRouter()
const tasks = ref([])
const loading = ref(false)
let pollingTimer = null

const taskStatusType = (s) => ({
  draft: 'info', pending: 'warning', running: '', completed: 'success', failed: 'danger', paused: 'warning', waiting_auth: 'warning', cancelled: 'info'
}[s] || 'info')

const taskStatusLabel = (s) => ({
  draft: '草稿', pending: '待执行', running: '执行中', completed: '已完成', failed: '失败', paused: '已暂停', waiting_auth: '等待认证', cancelled: '已取消'
}[s] || s)

const videoStatusType = (s) => ({
  pending: 'info', uploading: 'warning', processing: '', ready: '', published: 'success', failed: 'danger'
}[s] || 'info')

const videoStatusLabel = (s) => ({
  pending: '待发布', uploading: '上传中', processing: '处理中', ready: '就绪', published: '已发布', failed: '失败'
}[s] || s)

async function loadTasks() {
  loading.value = true
  try {
    const { data } = await taskApi.list()
    // 对每个任务加载详情（包含视频子任务状态）
    const tasksWithVideos = await Promise.all(
      data.map(async (t) => {
        try {
          const { data: detail } = await taskApi.get(t.id)
          return { ...t, videos: detail.videos || [] }
        } catch {
          return { ...t, videos: [] }
        }
      })
    )
    tasks.value = tasksWithVideos

    // 如果有正在运行的任务，启动轮询
    const hasRunning = tasks.value.some(t => t.status === 'running')
    if (hasRunning && !pollingTimer) {
      startPolling()
    } else if (!hasRunning && pollingTimer) {
      stopPolling()
    }
  } catch (e) {
    ElMessage.error('加载任务失败')
  } finally {
    loading.value = false
  }
}

function startPolling() {
  pollingTimer = setInterval(() => {
    loadTasks()
  }, 5000) // 每5秒刷新一次
}

function stopPolling() {
  if (pollingTimer) {
    clearInterval(pollingTimer)
    pollingTimer = null
  }
}

async function executeTask(row) {
  await ElMessageBox.confirm(`确定要执行任务 "${row.task_name}" 吗？系统将启动浏览器自动发布视频。`, '确认执行')
  try {
    const { data } = await publisherApi.execute(row.id)
    ElMessage.success(data.message)
    loadTasks()
  } catch (e) {
    ElMessage.error('执行失败: ' + (e.response?.data?.detail || e.message))
  }
}

async function resumeTask(row) {
  try {
    const { data } = await publisherApi.resume(row.id)
    ElMessage.success(data.message)
    loadTasks()
  } catch (e) {
    ElMessage.error('恢复失败')
  }
}

function viewLogs(row) {
  router.push({ path: '/logs', query: { task_id: row.id } })
}

async function deleteTask(row) {
  await ElMessageBox.confirm(`确定删除任务 "${row.task_name}" 吗？`, '确认删除', { type: 'warning' })
  try {
    await taskApi.delete(row.id)
    ElMessage.success('删除成功')
    loadTasks()
  } catch (e) {
    ElMessage.error('删除失败')
  }
}

onMounted(loadTasks)

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: #303133; }
h4 { color: #606266; font-weight: 600; }
</style>
