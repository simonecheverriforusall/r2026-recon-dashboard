ALTER TABLE public.comm_plan_quarter
  ADD COLUMN IF NOT EXISTS drive_folder_url TEXT;
