<template>
  <div>
    <div class="page-header">
      <h2>📋 账号管理</h2>
      <el-button type="primary" @click="showCreateDialog = true">
        <el-icon><Plus /></el-icon> 新增账号
      </el-button>
    </div>

    <!-- 账号列表 -->
    <el-table :data="accounts" stripe style="width: 100%" v-loading="loading">
      <el-table-column prop="name" label="账号名称" width="150" />
      <el-table-column prop="email" label="登录邮箱" width="220" />
      <el-table-column prop="tags" label="标签" width="150">
        <template #default="{ row }">
          <el-tag v-for="tag in (row.tags || '').split(',')" :key="tag" size="small"
                  style="margin-right:4px" v-if="tag">{{ tag }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="profile_url" label="账号主页" width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <a v-if="row.profile_url" :href="row.profile_url" target="_blank" style="color:#409eff;">
            {{ row.profile_url }}
          </a>
          <span v-else style="color:#999;">未设置</span>
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="pages_count" label="主页数量" width="100" />
      <el-table-column prop="is_logged_in" label="登录状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.is_logged_in ? 'success' : 'info'" size="small">
            {{ row.is_logged_in ? '已登录' : '未登录' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="320" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="managePages(row)">主页</el-button>
          <el-button size="small" type="primary" @click="loginAccount(row)"
                     :loading="loginLoadingId === row.id">登录</el-button>
          <el-button size="small" type="success" v-if="authWaitingId === row.id"
                     @click="confirmAuth(row)" :loading="authConfirming">认证完成</el-button>
          <el-button size="small" type="warning" @click="editAccount(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="deleteAccount(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新增账号对话框 -->
    <el-dialog v-model="showCreateDialog" :title="isEditing ? '编辑Facebook账号' : '新增Facebook账号'" width="500px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="邮箱">
          <el-input v-model="form.email" placeholder="Facebook登录邮箱或手机号" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="form.password" type="password" show-password placeholder="登录密码" />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="账号别名（便于识别）" />
        </el-form-item>
        <el-form-item label="账号主页">
          <el-input v-model="form.profile_url" placeholder="个人主页链接（如 https://www.facebook.com/xxx）" />
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="form.tags" placeholder="分类标签，逗号分隔" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" @click="isEditing ? updateAccount() : createAccount()" :loading="submitting">保存</el-button>
      </template>
    </el-dialog>

    <!-- 主页管理对话框 -->
    <el-dialog v-model="showPagesDialog" :title="`主页管理 - ${selectedAccount?.name || ''}`" width="750px">
      <div style="margin-bottom: 16px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
        <el-input v-model="newPageName" placeholder="主页名称" style="width: 160px;" />
        <el-input v-model="newPageUrl" placeholder="主页URL（可选）" style="width: 200px;" />
        <el-input v-model="newPageFbId" placeholder="Facebook ID（可选）" style="width: 160px;" />
        <el-button type="primary" @click="addPage" size="small">手动添加</el-button>
        <el-divider direction="vertical" />
        <el-button type="success" @click="fetchPages" size="small" :loading="fetchingPages">
          🔍 自动抓取主页
        </el-button>
      </div>
      <el-table :data="pages" stripe>
        <el-table-column prop="page_name" label="主页名称" min-width="150">
          <template #default="{ row }">
            <el-input v-if="editingPageId === row.id" v-model="editPageForm.page_name" size="small" />
            <span v-else>{{ row.page_name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="page_fb_id" label="Facebook ID" width="160">
          <template #default="{ row }">
            <el-input v-if="editingPageId === row.id" v-model="editPageForm.page_fb_id" size="small" />
            <span v-else>{{ row.page_fb_id || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="page_url" label="链接" show-overflow-tooltip>
          <template #default="{ row }">
            <el-input v-if="editingPageId === row.id" v-model="editPageForm.page_url" size="small" />
            <span v-else>{{ row.page_url || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="fan_count" label="粉丝" width="80" />
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <template v-if="editingPageId === row.id">
              <el-button size="small" type="success" text @click="savePageEdit(row)">保存</el-button>
              <el-button size="small" text @click="editingPageId = null">取消</el-button>
            </template>
            <template v-else>
              <el-button size="small" type="success" text
                         @click="loginPage(row)"
                         :loading="pageLoginLoadingId === row.id">登录</el-button>
              <el-button size="small" type="primary" text @click="startEditPage(row)">编辑</el-button>
              <el-button size="small" type="danger" text @click="removePage(row)">删除</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { accountApi, browserApi } from '../api/index.js'

const accounts = ref([])
const loading = ref(false)
const submitting = ref(false)
const showCreateDialog = ref(false)
const showPagesDialog = ref(false)
const selectedAccount = ref(null)
const pages = ref([])
const newPageName = ref('')
const newPageUrl = ref('')
const newPageFbId = ref('')
const fetchingPages = ref(false)
const editingPageId = ref(null)
const editPageForm = ref({ page_name: '', page_url: '', page_fb_id: '' })
const isEditing = ref(false)
const editingAccountId = ref(null)
const pageLoginLoadingId = ref(null)

const form = ref({ email: '', password: '', name: '', tags: '', profile_url: '' })
const loginLoadingId = ref(null)
const authWaitingId = ref(null)
const authConfirming = ref(false)
let authPollTimer = null

const statusType = (s) => ({ normal: 'success', pending_auth: 'warning', restricted: 'danger', banned: 'danger' }[s] || 'info')
const statusLabel = (s) => ({ normal: '正常', pending_auth: '待认证', restricted: '受限', banned: '封禁' }[s] || s)

async function loadAccounts() {
  loading.value = true
  try {
    const { data } = await accountApi.list()
    accounts.value = data
  } catch (e) {
    ElMessage.error('加载账号失败')
  } finally {
    loading.value = false
  }
}

async function createAccount() {
  submitting.value = true
  try {
    await accountApi.create(form.value)
    ElMessage.success('账号创建成功')
    showCreateDialog.value = false
    form.value = { email: '', password: '', name: '', tags: '', profile_url: '' }
    isEditing.value = false
    loadAccounts()
  } catch (e) {
    ElMessage.error('创建失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    submitting.value = false
  }
}

async function loginAccount(row) {
  loginLoadingId.value = row.id
  ElMessage.info('正在启动浏览器并登录...')
  try {
    const { data } = await browserApi.login(row.id)
    if (data.need_manual_auth) {
      ElMessage.warning(data.message)
      // 进入人工认证等待模式，显示"认证完成"按钮，并启动轮询
      authWaitingId.value = row.id
      startAuthPolling(row.id)
    } else if (data.success) {
      ElMessage.success(data.message)
      loadAccounts()
    } else {
      ElMessage.error(data.message)
    }
  } catch (e) {
    ElMessage.error('登录失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    loginLoadingId.value = null
  }
}

function startAuthPolling(accountId) {
  stopAuthPolling()
  authPollTimer = setInterval(async () => {
    try {
      const { data } = await browserApi.authStatus(accountId)
      if (data.logged_in) {
        ElMessage.success('检测到登录成功！')
        stopAuthPolling()
        authWaitingId.value = null
        loadAccounts()
      }
    } catch (e) {
      // 轮询出错不影响使用，静默忽略
    }
  }, 5000) // 每5秒检测一次
}

function stopAuthPolling() {
  if (authPollTimer) {
    clearInterval(authPollTimer)
    authPollTimer = null
  }
}

async function confirmAuth(row) {
  authConfirming.value = true
  try {
    const { data } = await browserApi.confirmAuth(row.id)
    if (data.success) {
      ElMessage.success(data.message)
      stopAuthPolling()
      authWaitingId.value = null
      loadAccounts()
    } else {
      ElMessage.warning(data.message)
    }
  } catch (e) {
    ElMessage.error('确认认证失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    authConfirming.value = false
  }
}

async function editAccount(row) {
  isEditing.value = true
  editingAccountId.value = row.id
  form.value = { email: row.email, password: '', name: row.name, tags: row.tags, profile_url: row.profile_url || '' }
  showCreateDialog.value = true
}

async function updateAccount() {
  submitting.value = true
  try {
    const data = { ...form.value }
    if (!data.password) delete data.password  // 不修改密码时不传
    await accountApi.update(editingAccountId.value, data)
    ElMessage.success('账号更新成功')
    showCreateDialog.value = false
    form.value = { email: '', password: '', name: '', tags: '', profile_url: '' }
    isEditing.value = false
    editingAccountId.value = null
    loadAccounts()
  } catch (e) {
    ElMessage.error('更新失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    submitting.value = false
  }
}

async function deleteAccount(row) {
  await ElMessageBox.confirm(`确定要删除账号 "${row.name}" 吗？`, '确认删除', { type: 'warning' })
  try {
    await accountApi.delete(row.id)
    ElMessage.success('删除成功')
    loadAccounts()
  } catch (e) {
    ElMessage.error('删除失败')
  }
}

async function managePages(row) {
  selectedAccount.value = row
  showPagesDialog.value = true
  try {
    const { data } = await accountApi.listPages(row.id)
    pages.value = data
  } catch (e) {
    ElMessage.error('加载主页失败')
  }
}

async function loginPage(row) {
  // 点击公共主页的"登录"按钮，在浏览器中导航到对应的公共主页链接
  const pageUrl = row.page_url || (row.page_fb_id ? `https://www.facebook.com/${row.page_fb_id}` : '')
  if (!pageUrl) {
    ElMessage.warning('该公共主页未设置链接和Facebook ID，无法导航')
    return
  }
  pageLoginLoadingId.value = row.id
  try {
    const { data } = await browserApi.navigatePage(selectedAccount.value.id, pageUrl)
    if (data.success) {
      ElMessage.success(`已跳转到公共主页: ${row.page_name}`)
    } else {
      ElMessage.error(data.message || '导航失败')
    }
  } catch (e) {
    ElMessage.error('导航失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    pageLoginLoadingId.value = null
  }
}

async function addPage() {
  if (!newPageName.value) return ElMessage.warning('请输入主页名称')
  try {
    await accountApi.addPage(selectedAccount.value.id, {
      page_name: newPageName.value,
      page_url: newPageUrl.value,
      page_fb_id: newPageFbId.value,
    })
    ElMessage.success('主页添加成功')
    newPageName.value = ''
    newPageUrl.value = ''
    newPageFbId.value = ''
    managePages(selectedAccount.value)
    loadAccounts()
  } catch (e) {
    ElMessage.error('添加失败')
  }
}

async function fetchPages() {
  fetchingPages.value = true
  try {
    const { data } = await accountApi.fetchPages(selectedAccount.value.id)
    ElMessage.success(data.message || '抓取完成')
    // 刷新主页列表
    managePages(selectedAccount.value)
    loadAccounts()
  } catch (e) {
    ElMessage.error('抓取失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    fetchingPages.value = false
  }
}

function startEditPage(row) {
  editingPageId.value = row.id
  editPageForm.value = {
    page_name: row.page_name,
    page_url: row.page_url || '',
    page_fb_id: row.page_fb_id || '',
  }
}

async function savePageEdit(row) {
  try {
    await accountApi.updatePage(row.id, editPageForm.value)
    ElMessage.success('主页更新成功')
    editingPageId.value = null
    managePages(selectedAccount.value)
    loadAccounts()
  } catch (e) {
    ElMessage.error('更新失败: ' + (e.response?.data?.detail || e.message))
  }
}

async function removePage(row) {
  await ElMessageBox.confirm(`确定删除主页 "${row.page_name}" 吗？`)
  try {
    await accountApi.removePage(row.id)
    ElMessage.success('已删除')
    managePages(selectedAccount.value)
    loadAccounts()
  } catch (e) {
    ElMessage.error('删除失败')
  }
}

onMounted(loadAccounts)
</script>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}
h2 { color: #303133; }
</style>