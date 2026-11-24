<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { ConfigProvider } from 'reka-ui'
import { Toaster } from 'vue-sonner'
import 'vue-sonner/style.css'
import AppLayout from './layouts/AppLayout.vue'
import { useTheme } from './composables/useTheme'
import AuthTransition from './components/AuthTransition.vue'

const route = useRoute()
const { theme } = useTheme()
const isPublic = computed(() => Boolean(route.meta.public))
</script>

<template>
  <ConfigProvider :scroll-body="false" locale="zh-CN">
    <RouterView v-if="isPublic" />
    <AppLayout v-else>
      <RouterView v-slot="{ Component }">
        <KeepAlive>
          <component :is="Component" />
        </KeepAlive>
      </RouterView>
    </AppLayout>
    <AuthTransition />
    <Toaster
      position="bottom-right"
      :theme="theme"
      :toast-options="{
        style: {
          borderRadius: '14px',
          border: '1px solid var(--border)',
          background: 'var(--popover)',
          color: 'var(--popover-foreground)',
          fontSize: '13px',
        },
      }"
    />
  </ConfigProvider>
</template>
