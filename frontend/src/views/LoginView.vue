<script setup lang="ts">
import { getErrorMessage } from '../utils/errors'
import { nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Eye, EyeOff, Loader2 } from 'lucide-vue-next'
import { useAppStore } from '../stores/app'
import BrandMark from '../components/BrandMark.vue'
import ThemeToggleButton from '../components/ThemeToggleButton.vue'
import Card from '../components/ui/Card.vue'
import Input from '../components/ui/Input.vue'
import Button from '../components/ui/Button.vue'
import { useAuthTransition } from '../composables/useAuthTransition'
import { markBrowserEntry } from '../utils/browserEntry'

const route = useRoute()
const router = useRouter()
const store = useAppStore()
const { authTransition, runAuthTransition } = useAuthTransition()

const password = ref('')
const showPassword = ref(false)
const loading = ref(false)
const error = ref('')
const passwordInput = ref<{ focus: (options?: FocusOptions) => void } | null>(null)

async function focusPassword() {
  await nextTick()
  if (!authTransition.visible) passwordInput.value?.focus({ preventScroll: true })
}
onMounted(focusPassword)
watch(() => authTransition.visible, (visible) => { if (!visible) focusPassword() })

async function submit() {
  if (!password.value || loading.value) return
  loading.value = true
  error.value = ''
  try {
    await store.login(password.value)
    markBrowserEntry()
    const next =
      typeof route.query.next === 'string' && route.query.next.startsWith('/') && !route.query.next.startsWith('//')
        ? route.query.next
        : '/'
    await runAuthTransition('login', () => router.replace(next))
  } catch (err: unknown) {
    error.value = getErrorMessage(err, '密码错误，请重试')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div :class="['auth-scene login-scene relative min-h-dvh flex flex-col items-center justify-center bg-background bg-dot-grid px-4 py-8 sm:py-12 overflow-hidden', { 'auth-scene-leaving': authTransition.visible && authTransition.kind === 'login' }]" :inert="authTransition.visible">
    <!-- Top-right theme switcher -->
    <div class="fixed top-4 right-4 z-10">
      <ThemeToggleButton
        size-class="h-8 w-8"
        icon-class="w-4 h-4"
        class="bg-card/85 backdrop-blur-sm"
      />
    </div>

    <div class="relative z-10 w-full max-w-[380px] space-y-6">
      <div class="flex flex-col items-center text-center space-y-2.5">
        <BrandMark class="w-12 h-12 drop-shadow-[0_4px_12px_rgba(0,0,0,0.14)]" />
        <div>
          <h1 class="text-xl font-bold tracking-tight text-foreground">登录 PH-DL</h1>
          <p class="text-sm text-muted-foreground mt-1">输入管理员密码以进入视频追更控制台</p>
        </div>
      </div>

      <Card class="p-5 sm:p-6">
        <form class="space-y-4" @submit.prevent="submit">
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">管理员密码</label>
            <div class="relative">
              <Input
                ref="passwordInput"
                v-model="password"
                :type="showPassword ? 'text' : 'password'"
                autofocus
                placeholder="请输入管理员密码"
                class="pr-9"
              />
              <button
                type="button"
                class="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground cursor-pointer"
                tabindex="-1"
                @click="showPassword = !showPassword"
              >
                <EyeOff v-if="showPassword" class="w-4 h-4" />
                <Eye v-else class="w-4 h-4" />
              </button>
            </div>
          </div>

          <div
            v-if="error"
            class="rounded-xl border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400"
          >
            {{ error }}
          </div>

          <Button type="submit" class="w-full" :disabled="loading || !password">
            <Loader2 v-if="loading" class="w-4 h-4 animate-spin" />
            <span>{{ loading ? '验证中...' : '继续' }}</span>
          </Button>
        </form>
      </Card>

      <p class="text-center text-xs text-muted-foreground">
        PH-DL · 纯净视频自动追更与云端同步
      </p>
    </div>
  </div>
</template>
