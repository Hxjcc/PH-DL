<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Download,
  ListChecks,
  RefreshCw,
  Database,
  Terminal,
  Settings,
  LogOut,
  HardDrive,
  Menu,
  X,
  PanelLeft,
  ChevronRight,
  UserRound,
} from 'lucide-vue-next'
import { useAppStore } from '../stores/app'
import { useAuthTransition } from '../composables/useAuthTransition'
import { formatBytes } from '../utils/format'
import BrandMark from '../components/BrandMark.vue'
import ThemeToggleButton from '../components/ThemeToggleButton.vue'
import Button from '../components/ui/Button.vue'
import Badge from '../components/ui/Badge.vue'
import Progress from '../components/ui/Progress.vue'

const route = useRoute()
const router = useRouter()
const store = useAppStore()
const { authTransition, runAuthTransition } = useAuthTransition()

const collapsed = ref(false)
const mobileOpen = ref(false)
const headerAvatarBroken = ref(false)

watch(
  () => store.accountAvatarUrl,
  () => {
    headerAvatarBroken.value = false
  }
)

watch(
  () => route.fullPath,
  () => {
    mobileOpen.value = false
  }
)

watch(mobileOpen, (open) => {
  if (typeof document === 'undefined') return
  document.body.style.overflow = open ? 'hidden' : ''
})

onBeforeUnmount(() => {
  if (typeof document !== 'undefined') {
    document.body.style.overflow = ''
  }
})

const navGroups = [
  {
    label: '工作台',
    items: [
      { name: 'download', path: '/', label: '新建下载', icon: Download },
      { name: 'jobs', path: '/jobs', label: '任务队列', icon: ListChecks },
      { name: 'targets', path: '/targets', label: '自动追更', icon: RefreshCw },
      { name: 'history', path: '/history', label: '历史记录', icon: Database },
    ],
  },
  {
    label: '系统',
    items: [
      { name: 'logs', path: '/logs', label: '实时日志', icon: Terminal },
      { name: 'settings', path: '/settings', label: '系统设置', icon: Settings },
    ],
  },
]

const allNavItems = navGroups.flatMap((g) => g.items)
const currentNav = computed(() => allNavItems.find((n) => n.name === route.name) || allNavItems[0])
const activeJobCount = computed(
  () => Number(store.overview?.queued_count || 0) + (store.overview?.running ? 1 : 0)
)

const diskInfo = computed(() => {
  const free = Number(store.overview?.disk?.free_bytes || 0)
  const total = Number(store.overview?.disk?.total_bytes || 0)
  const used = total > 0 ? Math.max(0, total - free) : 0
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0
  return {
    freeText: free > 0 ? formatBytes(free) : '—',
    totalText: total > 0 ? formatBytes(total) : '',
    pct,
  }
})

const cookieMeta = computed(() => {
  const ck = store.overview?.cookie
  if (!ck || !ck.configured) return { text: 'Cookie 未配置', dot: 'bg-amber-500 status-dot-glow-amber' }
  if (ck.valid === true) return { text: 'Cookie 有效', dot: 'bg-emerald-500 status-dot-glow-emerald' }
  if (ck.valid === false) return { text: 'Cookie 失效', dot: 'bg-red-500 status-dot-glow-red' }
  return { text: 'Cookie 已配置', dot: 'bg-zinc-400' }
})

async function handleLogout() {
  mobileOpen.value = false
  await runAuthTransition('logout', async () => {
    await store.logout()
    await router.push({ name: 'login' })
  })
}

onMounted(() => {
  if (!store.overviewLoaded) {
    store.refreshOverview()
  }
})
</script>

<template>
  <div :class="['auth-scene min-h-screen flex bg-background text-foreground', { 'auth-scene-leaving': authTransition.visible && authTransition.kind === 'logout' }]" :inert="authTransition.visible">
    <!-- Desktop Sidebar: crisp elevated rail with signature warm-orange active accent -->
    <aside
      :class="[
        'hidden md:flex flex-col shrink-0 overflow-hidden border-r border-border bg-sidebar sticky top-0 h-screen transition-[width] duration-200 ease-[cubic-bezier(0.22,1,0.36,1)]',
        collapsed ? 'w-[64px]' : 'w-[240px]',
      ]"
    >
      <!-- Brand (left-padded 16px so 32px icon is centered in 64px rail with zero horizontal jump) -->
      <div class="h-14 px-4 flex items-center gap-2.5 border-b border-border/80 shrink-0 overflow-hidden">
        <BrandMark class="w-8 h-8 shrink-0 drop-shadow-[0_1px_2px_rgba(0,0,0,0.12)]" />
        <div
          :class="[
            'w-[164px] shrink-0 flex items-center whitespace-nowrap transition-opacity duration-150',
            collapsed ? 'opacity-0 pointer-events-none' : 'opacity-100 delay-75',
          ]"
        >
          <div class="leading-none">
            <div class="text-[15px] font-bold tracking-tight text-foreground">PH-DL</div>
            <div class="text-[11px] text-muted-foreground mt-0.5">Video Sync</div>
          </div>
        </div>
      </div>

      <!-- Grouped Navigation -->
      <div
        :class="[
          'flex-1 overflow-y-auto overflow-x-hidden px-3.5 py-3 transition-all duration-200',
          collapsed ? 'space-y-1' : 'space-y-4',
        ]"
      >
        <div v-for="(group, groupIdx) in navGroups" :key="group.label" class="space-y-1">
          <div
            v-if="groupIdx > 0"
            :class="[
              'mx-1.5 bg-border/80 transition-all duration-200',
              collapsed ? 'my-2 h-px opacity-100' : 'my-0 h-0 opacity-0',
            ]"
          />
          <div
            :class="[
              'px-2.5 flex items-center text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/80 whitespace-nowrap overflow-hidden transition-all duration-200',
              collapsed ? 'h-0 opacity-0' : 'h-6 opacity-100 delay-75',
            ]"
          >
            {{ group.label }}
          </div>
          <RouterLink
            v-for="item in group.items"
            :key="item.name"
            :to="item.path"
            :title="collapsed ? item.label : undefined"
            :class="[
              'group relative flex items-center h-9 px-2.5 gap-2.5 rounded-md text-[13px] whitespace-nowrap overflow-hidden transition-all duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] active:scale-[0.98] no-underline',
              route.name === item.name
                ? [
                    'bg-secondary text-foreground font-medium',
                    collapsed ? 'ring-1 ring-border/80 shadow-2xs' : '',
                  ]
                : 'text-muted-foreground hover:text-foreground hover:bg-accent/80',
            ]"
          >
            <span
              v-if="route.name === item.name"
              :class="[
                'absolute left-0 top-2.5 bottom-2.5 w-[3px] rounded-r-full bg-brand transition-all duration-150',
                collapsed ? 'opacity-0 scale-y-0' : 'opacity-100 scale-y-100',
              ]"
            />
            <component
              :is="item.icon"
              :class="['w-4 h-4 shrink-0', route.name === item.name ? 'text-brand' : '']"
            />
            <span
              :class="[
                'flex-1 truncate transition-opacity duration-150',
                collapsed ? 'opacity-0 pointer-events-none' : 'opacity-100 delay-75',
              ]"
            >
              {{ item.label }}
            </span>
            <span
              v-if="item.name === 'jobs' && activeJobCount > 0"
              :class="[
                'shrink-0 bg-brand text-brand-foreground transition-opacity duration-150',
                collapsed
                  ? 'absolute top-1.5 right-1.5 w-1.5 h-1.5 rounded-full status-dot-glow-brand animate-pulse'
                  : 'min-w-[18px] h-[18px] px-1 rounded-full text-[10px] font-semibold tabular-nums flex items-center justify-center',
              ]"
            >
              {{ collapsed ? '' : activeJobCount }}
            </span>
          </RouterLink>
        </div>
      </div>

      <!-- Sidebar Footer -->
      <div class="p-3.5 border-t border-border/80 shrink-0 overflow-hidden">
        <div
          :class="[
            'overflow-hidden transition-all duration-200 ease-[cubic-bezier(0.22,1,0.36,1)]',
            collapsed ? 'max-h-0 opacity-0 mb-0' : 'max-h-28 opacity-100 mb-2',
          ]"
        >
          <div class="w-[211px] rounded-xl border border-border bg-background/70 dark:bg-card px-3.5 py-2.5 space-y-1.5">
            <div class="flex items-center justify-between text-xs whitespace-nowrap">
              <span class="inline-flex items-center gap-1.5 text-muted-foreground">
                <HardDrive class="w-3.5 h-3.5 shrink-0 text-brand/85" />
                可用空间
              </span>
              <span class="font-semibold text-foreground tabular-nums">{{ diskInfo.freeText }}</span>
            </div>
            <Progress v-if="diskInfo.totalText" :model-value="diskInfo.pct" class="h-1.5" />
            <div
              v-if="diskInfo.totalText"
              class="flex justify-between text-[11px] text-muted-foreground tabular-nums whitespace-nowrap"
            >
              <span>已用 {{ diskInfo.pct }}%</span>
              <span>{{ diskInfo.totalText }}</span>
            </div>
          </div>
        </div>

        <Button
          variant="ghost"
          size="sm"
          class="w-full h-9 justify-start px-2.5 gap-2.5 rounded-md text-muted-foreground hover:text-foreground overflow-hidden"
          :title="collapsed ? '退出登录' : undefined"
          @click="handleLogout"
        >
          <LogOut class="w-4 h-4 shrink-0" />
          <span
            :class="[
              'whitespace-nowrap transition-opacity duration-150',
              collapsed ? 'opacity-0' : 'opacity-100 delay-75',
            ]"
          >
            退出登录
          </span>
        </Button>
      </div>
    </aside>

    <!-- Main Layered Column -->
    <div class="flex-1 flex flex-col min-w-0 bg-background">
      <!-- Top Header Bar -->
      <header
        class="h-14 px-3 sm:px-4 md:px-6 border-b border-border bg-header backdrop-blur-md sticky top-0 z-20 flex items-center justify-between gap-2 sm:gap-3"
      >
        <div class="flex items-center gap-1.5 sm:gap-2 min-w-0">
          <Button
            variant="outline"
            size="icon"
            class="hidden md:inline-flex h-7 w-7 text-muted-foreground hover:text-foreground border-border/80"
            title="切换侧边栏"
            @click="collapsed = !collapsed"
          >
            <PanelLeft class="w-3.5 h-3.5" />
          </Button>

          <Button
            variant="ghost"
            size="icon"
            class="md:hidden h-8 w-8 -ml-1 text-muted-foreground shrink-0"
            aria-label="打开导航菜单"
            @click="mobileOpen = true"
          >
            <Menu class="w-4 h-4" />
          </Button>

          <div class="hidden md:block h-4 w-px bg-border mx-1" />

          <nav class="flex items-center gap-1.5 text-sm min-w-0">
            <span class="text-muted-foreground hidden sm:inline">PH-DL</span>
            <ChevronRight class="w-3.5 h-3.5 text-muted-foreground/60 hidden sm:inline" />
            <span class="font-medium text-foreground truncate">{{ currentNav.label }}</span>
          </nav>
        </div>

        <div class="flex items-center gap-1.5 sm:gap-2 shrink-0">
          <RouterLink v-if="store.overviewLoaded" to="/settings" class="no-underline">
            <Badge variant="outline" :dot="cookieMeta.dot" class="hover:bg-accent cursor-pointer h-7 px-2 sm:px-2.5 text-[11px] sm:text-xs">
              {{ cookieMeta.text }}
            </Badge>
          </RouterLink>

          <Badge
            variant="outline"
            :dot="
              store.wsConnected
                ? 'bg-emerald-500 status-dot-glow-emerald'
                : 'bg-zinc-400'
            "
            class="h-7 w-7 px-0 justify-center sm:w-auto sm:px-2.5"
            :title="store.wsConnected ? 'WebSocket 实时同步已连接' : '连接中断，正在重连'"
          >
            <span class="hidden sm:inline">{{ store.wsConnected ? '实时同步' : '重连中' }}</span>
          </Badge>

          <ThemeToggleButton />

          <RouterLink
            v-if="store.overviewLoaded && (store.accountAvatarUrl || store.accountUsername)"
            to="/settings"
            class="inline-flex h-7 w-7 sm:w-auto items-center justify-center sm:justify-start gap-1.5 rounded-full border border-border/80 bg-card p-0.5 sm:pl-0.5 sm:pr-2 text-xs text-foreground/90 no-underline transition-colors hover:bg-accent"
            :title="`${store.accountUsername || '本地账号'} · 点击进入系统设置更换头像`"
          >
            <span class="flex h-6 w-6 shrink-0 items-center justify-center overflow-hidden rounded-full bg-secondary text-muted-foreground">
              <img
                v-if="store.accountAvatarUrl && !headerAvatarBroken"
                :key="store.accountAvatarUrl"
                :src="store.accountAvatarUrl"
                alt=""
                class="h-full w-full object-cover"
                @error="headerAvatarBroken = true"
              />
              <UserRound v-else class="w-3.5 h-3.5" />
            </span>
            <span v-if="store.accountUsername" class="hidden sm:inline max-w-24 truncate font-medium">
              {{ store.accountUsername }}
            </span>
          </RouterLink>
        </div>
      </header>

      <!-- Main Content Canvas -->
      <main class="flex-1 w-full max-w-[1480px] mx-auto px-3.5 sm:px-5 md:px-8 lg:px-10 py-4 sm:py-6 md:py-7">
        <slot />
      </main>
    </div>

    <!-- Mobile Slide-Over Sheet -->
    <Teleport to="body">
      <div v-if="mobileOpen" class="fixed inset-0 z-50 md:hidden">
        <div
          class="fixed inset-0 bg-black/45 dark:bg-black/65 backdrop-blur-[2px] animate-overlay-in"
          @click="mobileOpen = false"
        />
        <aside
          class="fixed inset-y-0 left-0 h-dvh w-[min(17rem,82vw)] bg-sidebar border-r border-border flex flex-col shadow-xl overscroll-contain animate-drawer-in"
        >
          <div class="h-14 px-4 border-b border-border flex items-center justify-between shrink-0">
            <div class="flex items-center gap-2.5">
              <BrandMark class="w-8 h-8 shrink-0 drop-shadow-[0_1px_2px_rgba(0,0,0,0.12)]" />
              <div class="leading-none">
                <div class="font-bold text-sm text-foreground">PH-DL</div>
                <div class="text-[11px] text-muted-foreground mt-0.5">Video Sync</div>
              </div>
            </div>
            <Button variant="ghost" size="icon" class="h-8 w-8" aria-label="关闭菜单" @click="mobileOpen = false">
              <X class="w-4 h-4" />
            </Button>
          </div>

          <div class="flex-1 p-3 space-y-4 overflow-y-auto">
            <div v-for="group in navGroups" :key="group.label" class="space-y-1">
              <div class="px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/80">
                {{ group.label }}
              </div>
              <RouterLink
                v-for="item in group.items"
                :key="item.name"
                :to="item.path"
                :class="[
                  'relative flex items-center gap-2.5 px-3 h-10 rounded-lg text-sm transition-colors overflow-hidden no-underline',
                  route.name === item.name
                    ? 'bg-secondary text-foreground font-medium'
                    : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                ]"
                @click="mobileOpen = false"
              >
                <span
                  v-if="route.name === item.name"
                  class="absolute left-0 top-2.5 bottom-2.5 w-[3px] rounded-r-full bg-brand"
                />
                <component :is="item.icon" :class="['w-4 h-4', route.name === item.name ? 'text-brand' : '']" />
                <span>{{ item.label }}</span>
                <span
                  v-if="item.name === 'jobs' && activeJobCount > 0"
                  class="ml-auto min-w-[18px] h-[18px] px-1 rounded-full bg-brand text-brand-foreground text-[10px] font-semibold tabular-nums flex items-center justify-center"
                >
                  {{ activeJobCount }}
                </span>
              </RouterLink>
            </div>
          </div>

          <div class="p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] border-t border-border space-y-2.5 shrink-0">
            <div class="rounded-xl border border-border bg-background/70 dark:bg-card px-3 py-2 space-y-1.5">
              <div class="flex items-center justify-between text-xs">
                <span class="inline-flex items-center gap-1.5 text-muted-foreground">
                  <HardDrive class="w-3.5 h-3.5 shrink-0 text-brand/85" />
                  可用空间
                </span>
                <span class="font-semibold text-foreground tabular-nums">{{ diskInfo.freeText }}</span>
              </div>
              <Progress v-if="diskInfo.totalText" :model-value="diskInfo.pct" class="h-1.5" />
              <div
                v-if="diskInfo.totalText"
                class="flex justify-between text-[11px] text-muted-foreground tabular-nums"
              >
                <span>已用 {{ diskInfo.pct }}%</span>
                <span>{{ diskInfo.totalText }}</span>
              </div>
            </div>
            <Button variant="outline" size="sm" class="w-full" @click="handleLogout">
              <LogOut class="w-3.5 h-3.5" />
              退出登录
            </Button>
          </div>
        </aside>
      </div>
    </Teleport>
  </div>
</template>
