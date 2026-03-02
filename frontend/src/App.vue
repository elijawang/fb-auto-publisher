<template>
  <el-container class="app-container">
    <!-- 侧边栏导航 -->
    <el-aside width="220px" class="sidebar">
      <div class="logo">
        <el-icon :size="24"><VideoCamera /></el-icon>
        <span>FB自动发布</span>
      </div>
      <el-menu
        :default-active="$route.path"
        router
        background-color="#1a1a2e"
        text-color="#a0aec0"
        active-text-color="#667eea"
      >
        <el-menu-item index="/accounts">
          <el-icon><User /></el-icon>
          <span>账号管理</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><List /></el-icon>
          <span>任务管理</span>
        </el-menu-item>
        <el-menu-item index="/tasks/create">
          <el-icon><Plus /></el-icon>
          <span>创建任务</span>
        </el-menu-item>
        <el-menu-item index="/logs">
          <el-icon><Document /></el-icon>
          <span>执行日志</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <!-- 主内容区 -->
    <el-main class="main-content">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html, body, #app {
  height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}

.app-container {
  height: 100vh;
}

.sidebar {
  background-color: #1a1a2e;
  border-right: 1px solid #16213e;
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px;
  color: #667eea;
  font-size: 18px;
  font-weight: bold;
  border-bottom: 1px solid #16213e;
}

.main-content {
  background-color: #f5f7fa;
  padding: 24px;
}

.el-menu {
  border-right: none !important;
}
</style>
