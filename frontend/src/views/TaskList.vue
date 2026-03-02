<template>
  <div>
    <div class="page-header">
      <h2>📋 任务管理</h2>
      <div style="display: flex; align-items: center; gap: 12px;">
        <el-date-picker
          v-model="queryDate"
          type="date"
          placeholder="选择日期"
          format="YYYY-MM-DD"
          value-format="YYYY-MM-DD"
          @change="onDateChange"
          style="width: 160px;"
          clearable
        />
        <el-button @click="loadTasks" :loading="loading">🔄 刷新</el-button>
        <el-select v-model="queryGroupId" placeholder="全部分组" clearable @change="onGroupChange" style="width: 160px;">
          <el-option label="全部分组" value="" />
          <el-option v-for="group in groups" :key="group.id" :label="group.name" :value="group.id">
            <div style="display: flex; align-items: center; gap: 8px;">
              <div :style="{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: group.color }"></div>
              <span>{{ group.name }}</span>
            </div>
          </el-option>
        </el-select>
        <el-button type="primary" @click="$router.push('/tasks/create')">
          <el-icon><Plus /></el-icon> 创建任务
        </el-button>
      </div>
    </div>

    <el-table :data="tasks" stripe v-loading="loading" row-key="id">
      <!-- 展开行：视频子任务列表 -->
      <el-table-column type="expand">
        <template #default="{ row }">
          <div style="padding: 10px 20px;">
            <h4 style="margin-bottom: 10px;">📹 视频子任务（共 {{ row.videos ? row.videos.length : row.video_count }} 个）</h4>
            <!-- 按公共主页分组展示 -->
            <div v-for="(group, idx) in groupVideosByPage(row)" :key="idx" style="margin-bottom: 16px;">
              <div style="margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                <el-tag type="primary" size="small">📄 {{ group.pageName }}</el-tag>
                <template v-if="group.pageUrl">
                  <a :href="group.pageUrl" target="_blank" style="color: #409eff; text-decoration: none; font-size: 12px;">
                    🔗 {{ group.pageUrl }}
                  </a>
                </template>
                <span style="color: #909399; font-size: 12px;">（{{ group.videos.length }} 个视频）</span>
                <el-button
                  size="small"
                  type="warning"
                  @click="retryPageVideos(row, group)"
                  :disabled="!hasRetryableVideos(group) || row.status === 'running'"
                  :loading="group._retrying"
                >🔄 重新执行此主页</el-button>
              </div>
              <el-table :data="group.videos" size="small" border>
                <el-table-column prop="sequence" label="序号" width="60" />
                <el-table-column prop="file_name" label="文件名" show-overflow-tooltip />
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
                <el-table-column label="操作" width="100" fixed="right">
                  <template #default="{ row: v }">
                    <el-button
                      size="small"
                      type="warning"
                      @click="retryVideo(v)"
                      :disabled="v.status !== 'failed'"
                      :loading="v._retrying"
                    >重试</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <!-- 无公共主页分组时的回退展示 -->
            <div v-if="groupVideosByPage(row).length === 0 && row.videos && row.videos.length > 0">
              <el-table :data="row.videos" size="small" border>
                <el-table-column prop="sequence" label="序号" width="60" />
                <el-table-column prop="file_name" label="文件名" show-overflow-tooltip />
                <el-table-column prop="file_size" label="大小(MB)" width="90">
                  <template #default="{ row: v }">{{ v.file_size ? v.file_size.toFixed(2) : '-' }}</template>
                </el-table-column>
                <el-table-column prop="status" label="状态" width="120">
                  <template #default="{ row: v }">
                    <el-tag :type="videoStatusType(v.status)" size="small">{{ videoStatusLabel(v.status) }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="error_message" label="错误信息" show-overflow-tooltip />
                <el-table-column label="操作" width="100" fixed="right">
                  <template #default="{ row: v }">
                    <el-button
                      size="small"
                      type="warning"
                      @click="retryVideo(v)"
                      :disabled="v.status !== 'failed'"
                      :loading="v._retrying"
                    >重试</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </div>
        </template>
      </el-table-column>

      <el-table-column prop="task_name" label="任务名称" width="200" />
      <el-table-column label="账号" width="130" show-overflow-tooltip>
        <template #default="{ row }">
          <template v-if="row.account_profile_url">
            <a :href="row.account_profile_url" target="_blank" style="color: #409eff; text-decoration: none;">
              {{ row.account_name || '-' }}
            </a>
          </template>
          <template v-else>
            {{ row.account_name || '-' }}
          </template>
        </template>
      </el-table-column>
      <el-table-column label="账号主页" width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <template v-if="row.account_profile_url">
            <a :href="row.account_profile_url" target="_blank" style="color: #409eff; text-decoration: none; font-size: 12px;">
              {{ row.account_profile_url }}
            </a>
          </template>
          <template v-else>
            <span style="color: #999;">-</span>
          </template>
        </template>
      </el-table-column>
      <el-table-column label="分组" width="120">
        <template #default="{ row }">
          <div v-if="row.group_name" style="display: flex; align-items: center; gap: 6px;">
            <div :style="{ width: '10px', height: '10px', borderRadius: '50%', backgroundColor: row.group_color }"></div>
            <span>{{ row.group_name }}</span>
          </div>
          <span v-else style="color: #999;">未分组</span>
        </template>
      </el-table-column>
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
                     :disabled="row.status === 'running' || row.status === 'completed'">执行</el-button>
          <el-button size="small" type="warning" @click="resumeTask(row)"
                     v-if="row.status === 'paused' || row.status === 'waiting_auth'">恢复</el-button>
          <el-button size="small" @click="viewLogs(row)">日志</el-button>
          <el-button size="small" type="danger" @click="deleteTask(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <div style="margin-top: 16px; display: flex; justify-content: flex-end;">
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="total"
        layout="total, sizes, prev, pager, next, jumper"
        @size-change="onPageSizeChange"
        @current-change="onPageChange"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { taskApi, publisherApi, accountApi } from '../api/index.js'

const router = useRouter()
const tasks = ref([])
const groups = ref([])
const loading = ref(false)
let pollingTimer = null

// 日期查询（默认当天）
const queryDate = ref(new Date().toISOString().slice(0, 10))
const queryGroupId = ref('')

// 分页
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

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

// 从任务的视频子任务中按公共主页分组
function groupVideosByPage(task) {
  if (!task.videos || task.videos.length === 0) return []
  const groupMap = new Map()
  for (const v of task.videos) {
    const key = v.page_name || '(未分配主页)'
    if (!groupMap.has(key)) {
      groupMap.set(key, { pageName: key, pageUrl: v.page_url || '', videos: [], _retrying: false })
    }
    groupMap.get(key).videos.push(v)
  }
  return Array.from(groupMap.values())
}

// 判断分组中是否有可重试的视频（存在非 published 的视频）
function hasRetryableVideos(group) {
  return group.videos.some(v => v.status !== 'published')
}

// 加载分组列表
async function loadGroups() {
  try {
    const { data } = await accountApi.listGroups()
    groups.value = data
  } catch (e) {
    // 静默处理
  }
}

function onDateChange() {
  currentPage.value = 1
  loadTasks()
}

function onGroupChange() {
  currentPage.value = 1
  loadTasks()
}

function onPageChange(page) {
  currentPage.value = page
  loadTasks()
}

function onPageSizeChange(size) {
  pageSize.value = size
  currentPage.value = 1
  loadTasks()
}

async function loadTasks() {
  loading.value = true
  try {
    const params = {
      page: currentPage.value,
      page_size: pageSize.value,
    }
    if (queryDate.value) {
      params.date = queryDate.value
    }
    if (queryGroupId.value) {
      params.group_id = queryGroupId.value
    }
    const { data } = await taskApi.list(params)
    const taskItems = data.items || []
    total.value = data.total || 0

    // 对每个任务加载详情（包含视频子任务状态）
    const tasksWithVideos = await Promise.all(
      taskItems.map(async (t) => {
        try {
          const { data: detail } = await taskApi.get(t.id)
          return {
            ...t,
            account_name: detail.account_name || t.account_name || '',
            account_profile_url: detail.account_profile_url || t.account_profile_url || '',
            group_name: detail.group_name || t.group_name || null,
            group_color: detail.group_color || t.group_color || null,
            videos: (detail.videos || []).map(v => ({ ...v, _retrying: false })),
          }
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

async function retryVideo(video) {
  await ElMessageBox.confirm(
    `确定要重试视频 "${video.file_name}" (主页: ${video.page_name || '-'}) 吗？`,
    '确认重试'
  )
  video._retrying = true
  try {
    const { data } = await publisherApi.retryVideo(video.id)
    ElMessage.success(data.message)
    loadTasks()
  } catch (e) {
    ElMessage.error('重试失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    video._retrying = false
  }
}

async function retryPageVideos(task, group) {
  await ElMessageBox.confirm(
    `确定要重新执行主页 "${group.pageName}" 下的所有未成功视频子任务吗？`,
    '确认重新执行'
  )
  group._retrying = true
  try {
    const { data } = await publisherApi.retryPage(task.id, group.pageName)
    ElMessage.success(data.message)
    loadTasks()
  } catch (e) {
    ElMessage.error('重新执行失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    group._retrying = false
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

onMounted(() => {
  loadTasks()
  loadGroups()
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: #303133; }
h4 { color: #606266; font-weight: 600; }
</style>