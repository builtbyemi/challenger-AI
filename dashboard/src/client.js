import { createClient } from '@supabase/supabase-js'
import { validatePublicConfig } from './data'
const url = import.meta.env.VITE_SUPABASE_URL
const key = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY
export const supabase = validatePublicConfig(url, key) ? createClient(url, key) : null
