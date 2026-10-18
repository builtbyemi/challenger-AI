import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
export default defineConfig({ plugins: [react(), tailwindcss()], build: {rollupOptions: {output: {manualChunks: {charts:['recharts'], backend:['@supabase/supabase-js']}}}}, test: { environment: 'jsdom', setupFiles: './src/test-setup.js', exclude:['node_modules/**','e2e/**'] } })
