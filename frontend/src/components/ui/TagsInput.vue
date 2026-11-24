<script setup>
import { ref } from 'vue'
import { X } from 'lucide-vue-next'
import { cn } from '../../lib/utils'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  placeholder: { type: String, default: '输入后按回车添加' },
  commitKeys: { type: Array, default: () => ['Enter', ',', '，'] },
  parseDraft: { type: Function, default: null },
  class: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])
const draft = ref('')
const inputRef = ref(null)

function focusInput() {
  inputRef.value?.focus()
}

function commit() {
  const raw = draft.value.trim()
  if (!raw) return
  if (typeof props.parseDraft === 'function') {
    const res = props.parseDraft(raw, props.modelValue)
    if (res === false) return
    if (Array.isArray(res)) {
      emit('update:modelValue', Array.from(new Set(res)))
    }
    draft.value = ''
    return
  }
  const parts = raw
    .split(/[,，\s]+/)
    .map((part) => part.trim())
    .filter(Boolean)
  if (parts.length === 0) return
  emit('update:modelValue', Array.from(new Set([...props.modelValue, ...parts])))
  draft.value = ''
}

function removeTag(tag) {
  emit('update:modelValue', props.modelValue.filter((item) => item !== tag))
}

function handleKeydown(event) {
  if (props.commitKeys.includes(event.key)) {
    event.preventDefault()
    commit()
    return
  }
  if (event.key === 'Backspace' && !draft.value && props.modelValue.length > 0) {
    removeTag(props.modelValue[props.modelValue.length - 1])
  }
}
</script>

<template>
  <div
    :class="
      cn(
        'flex min-h-9 w-full flex-wrap items-center gap-1.5 rounded-lg border border-input bg-card px-2 py-1.5 shadow-xs transition-all duration-150 focus-within:border-foreground/35 focus-within:ring-[3px] focus-within:ring-foreground/10',
        props.class
      )
    "
    @click="focusInput"
  >
    <span
      v-for="tag in modelValue"
      :key="tag"
      class="inline-flex h-5 max-w-full items-center gap-1 rounded-full bg-secondary px-2 text-[11px] text-secondary-foreground"
    >
      <span class="truncate">{{ tag }}</span>
      <button
        type="button"
        class="shrink-0 rounded-full text-muted-foreground hover:text-foreground cursor-pointer"
        :title="`移除 ${tag}`"
        @click.stop="removeTag(tag)"
      >
        <X class="h-3 w-3" />
      </button>
    </span>
    <input
      ref="inputRef"
      v-model="draft"
      type="text"
      :placeholder="modelValue.length === 0 ? placeholder : ''"
      class="h-6 min-w-[90px] flex-1 bg-transparent px-1 text-sm text-foreground outline-none placeholder:text-muted-foreground"
      @keydown="handleKeydown"
      @blur="commit"
    />
  </div>
</template>
