<script setup lang="ts">
// Rendering is handled by the SVG template.
</script>

<template>
  <svg class="smooth-spinner" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8" opacity="0.16" />
    <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-dasharray="38 19" />
  </svg>
</template>

<style scoped>
.smooth-spinner {
  display: block;
  shape-rendering: geometricPrecision;
  transform-origin: center;
  animation: smooth-spin 900ms linear infinite;
}
@keyframes smooth-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .smooth-spinner { animation: none; } }
</style>
