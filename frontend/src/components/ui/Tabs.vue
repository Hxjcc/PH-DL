<script setup>
import { TabsRoot, TabsList, TabsTrigger } from 'reka-ui'
import { cn } from '../../lib/utils'
import Button from './Button.vue'

const props = defineProps({
  modelValue: { type: String, required: true },
  items: { type: Array, default: () => [] }, // [{ value, label, icon? }]
  class: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <TabsRoot :model-value="modelValue" @update:model-value="emit('update:modelValue', $event)">
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
