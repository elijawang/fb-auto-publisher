<template>
  <div>
    <h2>📊 执行日志</h2>

    <!-- 筛选栏 -->
    <el-card style="margin: 16px 0;">
      <el-form :inline="true" :model="filters" size="default">
        <el-form-item label="账号">
          <el-input v-model="filters.account_name" placeholder="搜索账号" clearable />
        </el-form-item>
        <el-form-item label="主页">
          <el-input v-model="filters.page_name" placeholder="搜索主页" clearable />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="filters.status" placeholder="全部" clearable>
            <el-option label="已发布" value="published" />
            <el-option label="失败" value="failed" />
            <el-option label="待发布" value="pending" />
            <el-option label="上传中" value="uploading" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="loadLogs">查询</el-button>
          <el-button @click="loadSummary" v-if="filters.task_id">统计</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 统计摘要 -->
    <el-row :gutter="16" v-if="summary" style="margin-bottom: 16px;">
      <el-col :span="6">
        <el-statistic title="总计" :value="summary.total" />
      </el-col>
      <el-col :span="6">
        <el-statistic title="已发布" :value="summary.published" style="color:#67c23a" />
      </el-col>
      <el-col :span="6">
        <el-statistic title="失败" :value="summary.failed" style="color:#f56c6c" />
      </el-col>
      <el-col :span="6">
        <el-statistic title="成功率" :value="summary.success_rate" suffix="%" />
      </el-col>
    </el-row>

    <!-- 日志列表 -->
    <el-table :data="logs" stripe v-loading="loading">
      <el-table-column prop="account_name" label="账号" width="120" />
      <el-table-column prop="page_name" label="主页" width="150" />
      <el-table-column prop="video_file_name" label="视频文件" show-overflow-tooltip />
      <el-table-column prop="scheduled_time" label="计划时间" width="180">
        <template #default="{ row }">
          {{ row.scheduled_time ? new Date(row.scheduled_time).toLocaleString('zh-CN') : '-' }}
        </template>
      </el-table-column>
      <el-table-column prop="actual_time" label="实际时间" width="180">
        <template #default="{ row }">
          {{ row.actual_time ? new Date(row.actual_time).toLocaleString('zh-CN') : '-' }}
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="logStatusType(row.status)">{{ logStatusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="error_message" label="错误信息" show-overflow-tooltip />
    </el-table>

    <!-- 分页 -->
    <el-pagination
      style="margin-top: 16px; justify-content: flex-end;"
      layout="total, prev, pager, next"
      :total="total"
      :page-size="pageSize"
      v-model:current-page="currentPage"
      @current-change="loadLogs"
    />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { logApi } from '../api/index.js'

const route = useRoute()
const logs = ref([])
const loading = ref(false)
const summary = ref(null)
const currentPage = ref(1)
const pageSize = ref(50)
const total = ref(0)

const filters = ref({
  task_id: route.query.task_id || '',
  account_name: '',
  page_name: '',
  status: '',
})

const logStatusType = (s) => ({
  published: 'success', failed: 'danger', pending: 'info', uploading: 'warning'
}[s] || 'info')

const logStatusLabel = (s) => ({
  published: '已发布', failed: '失败', pending: '待发布', uploading: '上传中'
}[s] || s)

async function loadLogs() {
  loading.value = true
  try {
    const { data } = await logApi.list({
      ...filters.value,
      limit: pageSize.value,
      offset: (currentPage.value - 1) * pageSize.value,
    })
    logs.value = data
    total.value = data.length >= pageSize.value ? currentPage.value * pageSize.value + 1 : (currentPage.value - 1) * pageSize.value + data.length
  } catch (e) {
    ElMessage.error('加载日志失败')
  } finally {
    loading.value = false
  }
}

async function loadSummary() {
  if (!filters.value.task_id) return
  try {
    const { data } = await logApi.summary(filters.value.task_id)
    summary.value = data
  } catch (e) {
    ElMessage.error('加载统计失败')
  }
}

onMounted(() => {
  loadLogs()
  if (filters.value.task_id) loadSummary()
})
</script>

<style scoped>
h2 { color: #303133; margin-bottom: 12px; }
</style>
