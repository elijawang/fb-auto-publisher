<template>
  <div>
    <h2>📝 创建发布任务</h2>
    <el-card style="max-width: 800px; margin-top: 20px;">
      <el-form :model="form" label-width="120px" size="large">
        <!-- 选择账号 -->
        <el-form-item label="选择账号" required>
          <el-select v-model="form.account_id" placeholder="选择Facebook账号" style="width: 100%">
            <el-option v-for="acc in accounts" :key="acc.id" :label="`${acc.name} (${acc.email})`" :value="acc.id" />
          </el-select>
        </el-form-item>

        <!-- 任务名称 -->
        <el-form-item label="任务名称" required>
          <el-input v-model="form.task_name" placeholder="例如：2026新年视频发布" />
        </el-form-item>

        <!-- 视频描述 -->
        <el-form-item label="视频描述" required>
          <el-input v-model="form.description" type="textarea" :rows="4"
                    placeholder="所有视频共用的描述文本，支持Emoji和#话题标签" />
        </el-form-item>

        <!-- 起始发布时间 -->
        <el-form-item label="起始发布时间" required>
          <el-date-picker v-model="form.start_time" type="datetime" placeholder="选择起始时间"
                          format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DDTHH:mm:ss"
                          style="width: 100%" />
        </el-form-item>

        <!-- 发布间隔 -->
        <el-form-item label="发布间隔" required>
          <el-input-number v-model="form.interval_minutes" :min="1" :max="1440" />
          <span style="margin-left: 10px; color: #909399;">分钟（60 = 1小时）</span>
        </el-form-item>

        <!-- 上传视频 -->
        <el-form-item label="上传视频">
          <el-upload
            ref="uploadRef"
            :auto-upload="false"
            :on-change="handleFileChange"
            :on-remove="handleFileRemove"
            :file-list="fileList"
            multiple
            accept="video/*"
            drag
          >
            <el-icon :size="40"><Upload /></el-icon>
            <div>将视频文件拖拽到此处，或 <em>点击上传</em></div>
            <template #tip>
              <div style="color:#909399; margin-top:8px;">支持 MP4、MOV 等视频格式，可批量上传</div>
            </template>
          </el-upload>
        </el-form-item>

        <!-- 发布预览 -->
        <el-form-item label="发布预览" v-if="fileList.length > 0 && form.start_time">
          <el-timeline>
            <el-timeline-item v-for="(item, i) in previewSchedule" :key="i"
                              :timestamp="item.time" placement="top">
              视频{{ item.seq }}：{{ item.name }}
            </el-timeline-item>
          </el-timeline>
        </el-form-item>

        <!-- 提交 -->
        <el-form-item>
          <el-button type="primary" size="large" @click="submitTask" :loading="submitting">
            创建任务
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { accountApi, taskApi } from '../api/index.js'

const router = useRouter()
const accounts = ref([])
const fileList = ref([])
const submitting = ref(false)
const uploadRef = ref(null)

const form = ref({
  account_id: '',
  task_name: '',
  description: '',
  start_time: '',
  interval_minutes: 60,
})

const previewSchedule = computed(() => {
  if (!form.value.start_time || fileList.value.length === 0) return []
  const start = new Date(form.value.start_time)
  return fileList.value.map((f, i) => {
    const t = new Date(start.getTime() + form.value.interval_minutes * 60000 * i)
    return {
      seq: i + 1,
      name: f.name,
      time: t.toLocaleString('zh-CN'),
    }
  })
})

function handleFileChange(file, list) {
  fileList.value = list
}

function handleFileRemove(file, list) {
  fileList.value = list
}

async function submitTask() {
  if (!form.value.account_id) return ElMessage.warning('请选择账号')
  if (!form.value.task_name) return ElMessage.warning('请输入任务名称')
  if (!form.value.start_time) return ElMessage.warning('请选择起始时间')
  if (fileList.value.length === 0) return ElMessage.warning('请上传至少一个视频')

  submitting.value = true
  try {
    // 1. 创建任务
    const { data: taskData } = await taskApi.create(form.value)
    const taskId = taskData.id

    // 2. 上传视频
    const formData = new FormData()
    fileList.value.forEach(f => {
      formData.append('files', f.raw)
    })
    await taskApi.uploadVideos(taskId, formData)

    ElMessage.success('任务创建成功！')
    router.push('/tasks')
  } catch (e) {
    ElMessage.error('创建失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  try {
    const { data } = await accountApi.list()
    accounts.value = data
  } catch (e) {
    ElMessage.error('加载账号失败')
  }
})
</script>

<style scoped>
h2 { color: #303133; margin-bottom: 12px; }
</style>
