<script setup>
import { computed } from 'vue'
import { useAuthTransition } from '../composables/useAuthTransition'
import BrandMark from './BrandMark.vue'
import Spinner from './ui/Spinner.vue'

const { authTransition } = useAuthTransition()
const message = computed(() => {
  if (authTransition.kind === 'entry') return { heading: 'PH-DL', detail: '正在进入工作台' }
  if (authTransition.kind === 'login') return { heading: '登录成功', detail: '正在进入 PH-DL' }
  return {
    heading: authTransition.phase === 'cover' ? '正在退出登录' : '已退出登录',
    detail: '正在返回登录页面',
  }
})
</script>

<template>
  <Teleport to="body">
    <div v-if="authTransition.visible" :class="['auth-cover', `auth-cover-${authTransition.phase}`]" role="status" aria-live="polite">
      <div class="flex flex-col items-center gap-4 text-center">
        <BrandMark class="w-14 h-14" />
        <div class="text-lg font-semibold tracking-tight">{{ message.heading }}</div>
        <div class="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner class="w-4 h-4" />
          {{ message.detail }}
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.auth-cover {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: grid;
  place-items: center;
  background: var(--background);
  color: var(--foreground);
}
.auth-cover-cover { animation: auth-cover-in 280ms cubic-bezier(0.22, 1, 0.36, 1) both; }
.auth-cover-reveal { animation: auth-cover-out 360ms cubic-bezier(0.65, 0, 0.35, 1) both; }
@keyframes auth-cover-in { from { clip-path: inset(100% 0 0); } to { clip-path: inset(0); } }
@keyframes auth-cover-out { from { clip-path: inset(0); } to { clip-path: inset(0 0 100%); } }
@media (prefers-reduced-motion: reduce) { .auth-cover-cover, .auth-cover-reveal { animation: none; } }
</style>
