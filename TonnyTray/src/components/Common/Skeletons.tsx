/**
 * Skeleton loaders for async content
 * Provides loading placeholders for better UX
 */

import { Box, Paper, Skeleton, Stack } from '@mui/material';

/**
 * Skeleton for status panel
 */
export function StatusPanelSkeleton() {
  return (
    <Paper sx={{ p: 3 }}>
      <Stack spacing={2}>
        <Skeleton variant="text" width="40%" height={32} />
        <Stack spacing={1.5}>
          <Skeleton variant="rectangular" height={60} />
          <Skeleton variant="rectangular" height={60} />
          <Skeleton variant="rectangular" height={60} />
        </Stack>
      </Stack>
    </Paper>
  );
}

/**
 * Skeleton for transcription panel
 */
export function TranscriptionPanelSkeleton() {
  return (
    <Paper sx={{ p: 3 }}>
      <Stack spacing={2}>
        <Skeleton variant="text" width="50%" height={32} />
        <Box>
          <Skeleton variant="text" width="30%" height={20} />
          <Skeleton variant="rectangular" height={80} sx={{ mt: 1 }} />
        </Box>
        <Skeleton variant="text" width="40%" height={20} />
      </Stack>
    </Paper>
  );
}

/**
 * Skeleton for profile selector
 */
export function ProfileSelectorSkeleton() {
  return (
    <Paper sx={{ p: 2 }}>
      <Stack direction="row" spacing={2} alignItems="center">
        <Skeleton variant="circular" width={48} height={48} />
        <Stack spacing={0.5} sx={{ flex: 1 }}>
          <Skeleton variant="text" width="30%" height={24} />
          <Skeleton variant="text" width="20%" height={20} />
        </Stack>
        <Skeleton variant="rectangular" width={100} height={36} />
      </Stack>
    </Paper>
  );
}

/**
 * Skeleton for quick actions
 */
export function QuickActionsSkeleton() {
  return (
    <Paper sx={{ p: 3 }}>
      <Skeleton variant="text" width="30%" height={32} sx={{ mb: 2 }} />
      <Stack direction="row" spacing={2}>
        {[1, 2, 3, 4].map((i) => (
          <Box key={i} sx={{ flex: 1 }}>
            <Skeleton variant="rectangular" height={100} sx={{ borderRadius: 2 }} />
            <Skeleton variant="text" width="60%" sx={{ mt: 1 }} />
          </Box>
        ))}
      </Stack>
    </Paper>
  );
}

/**
 * Skeleton for log entry
 */
export function LogEntrySkeleton() {
  return (
    <Box sx={{ p: 2, borderBottom: 1, borderColor: 'divider' }}>
      <Stack direction="row" spacing={2} alignItems="flex-start">
        <Skeleton variant="rectangular" width={60} height={20} />
        <Stack spacing={0.5} sx={{ flex: 1 }}>
          <Skeleton variant="text" width="80%" />
          <Skeleton variant="text" width="60%" />
        </Stack>
        <Skeleton variant="text" width={100} />
      </Stack>
    </Box>
  );
}

/**
 * Skeleton for statistics card
 */
export function StatisticsCardSkeleton() {
  return (
    <Paper sx={{ p: 3 }}>
      <Stack spacing={2}>
        <Skeleton variant="text" width="50%" height={24} />
        <Skeleton variant="text" width="70%" height={48} />
        <Skeleton variant="text" width="40%" height={20} />
      </Stack>
    </Paper>
  );
}

/**
 * Skeleton for list of items
 */
export function ListSkeleton({ count = 5 }: { count?: number }) {
  return (
    <Stack spacing={1}>
      {Array.from({ length: count }).map((_, i) => (
        <Box key={i} sx={{ p: 2, borderBottom: 1, borderColor: 'divider' }}>
          <Stack direction="row" spacing={2} alignItems="center">
            <Skeleton variant="circular" width={40} height={40} />
            <Stack spacing={0.5} sx={{ flex: 1 }}>
              <Skeleton variant="text" width="60%" />
              <Skeleton variant="text" width="40%" />
            </Stack>
            <Skeleton variant="rectangular" width={80} height={32} />
          </Stack>
        </Box>
      ))}
    </Stack>
  );
}

/**
 * Generic content skeleton
 */
export function ContentSkeleton() {
  return (
    <Paper sx={{ p: 3 }}>
      <Stack spacing={2}>
        <Skeleton variant="text" width="40%" height={32} />
        <Skeleton variant="rectangular" height={200} />
        <Stack direction="row" spacing={2}>
          <Skeleton variant="rectangular" height={40} sx={{ flex: 1 }} />
          <Skeleton variant="rectangular" height={40} sx={{ flex: 1 }} />
        </Stack>
      </Stack>
    </Paper>
  );
}
