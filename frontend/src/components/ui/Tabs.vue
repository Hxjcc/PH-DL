<script setup lang="ts" generic="T extends string">
import type { Component } from 'vue'
import type { ClassValue } from 'clsx'
import { TabsRoot, TabsList, TabsTrigger, type AcceptableValue } from 'reka-ui'
import { cn } from '../../lib/utils'
import Button from './Button.vue'

const props = withDefaults(defineProps<{
  modelValue: T & string
  items?: readonly { value: T; label: string; icon?: Component }[]
  class?: ClassValue
}>(), {
  items: () => [],
  class: '',
})

const emit = defineEmits<{ 'update:modelValue': [value: T] }>()

function onUpdate(value: AcceptableValue) {
  emit('update:modelValue', value as T)
}
</script>

<template>
  <TabsRoot :model-value="modelValue" @update:model-value="onUpdate">
    <TabsList
      :class="
        cn(
          'inline-flex h-10 items-center justify-center rounded-xl bg-muted p-1 text-muted-foreground',
          props.class
        )
      "
    >
      <TabsTrigger
        v-for="item in items"
        :key="item.value"
        :value="item.value"
        as-child
      >
        <Button
          variant="ghost"
          size="sm"
          class="gap-1.5 px-2.5 sm:px-3.5 whitespace-nowrap text-muted-foreground hover:bg-brand/8 hover:text-foreground data-[state=active]:bg-card data-[state=active]:text-foreground data-[state=active]:shadow-xs"
        >
          <component :is="item.icon" v-if="item.icon" class="w-3.5 h-3.5" />
          <span>{{ item.label }}</span>
        </Button>
      </TabsTrigger>
    </TabsList>
  </TabsRoot>
</template>
