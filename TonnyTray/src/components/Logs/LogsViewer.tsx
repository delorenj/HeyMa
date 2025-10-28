/**
 * Comprehensive logs viewer component
 * Filterable, searchable log display with export functionality
 */

import { useState, useMemo } from 'react';
import {
  Box,
  Paper,
  Typography,
  Stack,
  TextField,
  Select,
  MenuItem,
  Button,
  Chip,
  IconButton,
  InputAdornment,
  FormControl,
  InputLabel,
  Tooltip,
  Alert,
} from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import ClearIcon from '@mui/icons-material/Clear';
import DownloadIcon from '@mui/icons-material/Download';
import DeleteIcon from '@mui/icons-material/Delete';
import RefreshIcon from '@mui/icons-material/Refresh';
import FilterListIcon from '@mui/icons-material/FilterList';
import { motion, AnimatePresence } from 'framer-motion';
import { useAppStore } from '@hooks/useTauriState';
import { useDebounce } from '@hooks/useDebounce';
import { useConfirmDialog } from '@components/Common/ConfirmDialog';
import { useNotification } from '@hooks/useNotification';
import { tauriApi } from '@services/tauri';
import { formatDateTime } from '@utils/formatters';
import type { LogEntry } from '@types';

type LogLevel = 'debug' | 'info' | 'warn' | 'error' | 'all';

function getLevelColor(level: LogEntry['level']): string {
  switch (level) {
    case 'debug':
      return 'info';
    case 'info':
      return 'primary';
    case 'warn':
      return 'warning';
    case 'error':
      return 'error';
    default:
      return 'default';
  }
}

export default function LogsViewer() {
  const { logs } = useAppStore();
  const [searchTerm, setSearchTerm] = useState('');
  const [levelFilter, setLevelFilter] = useState<LogLevel>('all');
  const [componentFilter, setComponentFilter] = useState<string>('all');
  const { showConfirm, dialog } = useConfirmDialog();
  const { showSuccess, showError } = useNotification();

  const debouncedSearch = useDebounce(searchTerm, 300);

  // Get unique components for filter
  const uniqueComponents = useMemo(() => {
    const components = new Set(logs.map((log) => log.component));
    return Array.from(components).sort();
  }, [logs]);

  // Filter logs
  const filteredLogs = useMemo(() => {
    let filtered = logs;

    // Level filter
    if (levelFilter !== 'all') {
      filtered = filtered.filter((log) => log.level === levelFilter);
    }

    // Component filter
    if (componentFilter !== 'all') {
      filtered = filtered.filter((log) => log.component === componentFilter);
    }

    // Search filter
    if (debouncedSearch) {
      const searchLower = debouncedSearch.toLowerCase();
      filtered = filtered.filter(
        (log) =>
          log.message.toLowerCase().includes(searchLower) ||
          log.component.toLowerCase().includes(searchLower) ||
          (log.metadata &&
            JSON.stringify(log.metadata).toLowerCase().includes(searchLower))
      );
    }

    return filtered;
  }, [logs, levelFilter, componentFilter, debouncedSearch]);

  const handleClearLogs = async () => {
    await showConfirm({
      title: 'Clear All Logs?',
      message: 'This will permanently delete all log entries. This action cannot be undone.',
      severity: 'warning',
      confirmText: 'Clear Logs',
      confirmColor: 'error',
      onConfirm: async () => {
        try {
          await tauriApi.logs.clear();
          showSuccess('Logs cleared successfully');
        } catch (error) {
          showError(`Failed to clear logs: ${error}`);
        }
      },
    });
  };

  const handleExportLogs = async () => {
    try {
      // Use Tauri save dialog
      const path = await tauriApi.logs.export('logs.txt');
      showSuccess(`Logs exported to ${path}`);
    } catch (error) {
      showError(`Failed to export logs: ${error}`);
    }
  };

  const handleRefresh = async () => {
    try {
      await tauriApi.logs.get();
      // Store update happens through events
      showSuccess('Logs refreshed');
    } catch (error) {
      showError(`Failed to refresh logs: ${error}`);
    }
  };

  return (
    <Paper elevation={2} sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* Header */}
      <Box sx={{ p: 2, borderBottom: 1, borderColor: 'divider' }}>
        <Stack spacing={2}>
          <Stack direction="row" alignItems="center" justifyContent="space-between">
            <Typography variant="h6">Logs Viewer</Typography>
            <Stack direction="row" spacing={1}>
              <Tooltip title="Refresh logs">
                <IconButton size="small" onClick={handleRefresh}>
                  <RefreshIcon />
                </IconButton>
              </Tooltip>
              <Tooltip title="Export logs">
                <IconButton size="small" onClick={handleExportLogs}>
                  <DownloadIcon />
                </IconButton>
              </Tooltip>
              <Tooltip title="Clear all logs">
                <IconButton size="small" onClick={handleClearLogs} color="error">
                  <DeleteIcon />
                </IconButton>
              </Tooltip>
            </Stack>
          </Stack>

          {/* Filters */}
          <Stack direction="row" spacing={2} alignItems="center">
            {/* Search */}
            <TextField
              size="small"
              placeholder="Search logs..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              sx={{ flex: 1 }}
              InputProps={{
                startAdornment: (
                  <InputAdornment position="start">
                    <SearchIcon fontSize="small" />
                  </InputAdornment>
                ),
                endAdornment: searchTerm && (
                  <InputAdornment position="end">
                    <IconButton size="small" onClick={() => setSearchTerm('')}>
                      <ClearIcon fontSize="small" />
                    </IconButton>
                  </InputAdornment>
                ),
              }}
            />

            {/* Level filter */}
            <FormControl size="small" sx={{ minWidth: 120 }}>
              <InputLabel>Level</InputLabel>
              <Select
                value={levelFilter}
                label="Level"
                onChange={(e) => setLevelFilter(e.target.value as LogLevel)}
              >
                <MenuItem value="all">All Levels</MenuItem>
                <MenuItem value="debug">Debug</MenuItem>
                <MenuItem value="info">Info</MenuItem>
                <MenuItem value="warn">Warning</MenuItem>
                <MenuItem value="error">Error</MenuItem>
              </Select>
            </FormControl>

            {/* Component filter */}
            <FormControl size="small" sx={{ minWidth: 150 }}>
              <InputLabel>Component</InputLabel>
              <Select
                value={componentFilter}
                label="Component"
                onChange={(e) => setComponentFilter(e.target.value)}
              >
                <MenuItem value="all">All Components</MenuItem>
                {uniqueComponents.map((component) => (
                  <MenuItem key={component} value={component}>
                    {component}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Stack>

          {/* Active filters */}
          {(levelFilter !== 'all' || componentFilter !== 'all' || searchTerm) && (
            <Stack direction="row" spacing={1} alignItems="center">
              <FilterListIcon fontSize="small" color="action" />
              {levelFilter !== 'all' && (
                <Chip
                  size="small"
                  label={`Level: ${levelFilter}`}
                  onDelete={() => setLevelFilter('all')}
                />
              )}
              {componentFilter !== 'all' && (
                <Chip
                  size="small"
                  label={`Component: ${componentFilter}`}
                  onDelete={() => setComponentFilter('all')}
                />
              )}
              {searchTerm && (
                <Chip
                  size="small"
                  label={`Search: "${searchTerm}"`}
                  onDelete={() => setSearchTerm('')}
                />
              )}
              <Button
                size="small"
                onClick={() => {
                  setLevelFilter('all');
                  setComponentFilter('all');
                  setSearchTerm('');
                }}
              >
                Clear All
              </Button>
            </Stack>
          )}

          {/* Result count */}
          <Typography variant="caption" color="text.secondary">
            Showing {filteredLogs.length} of {logs.length} log entries
          </Typography>
        </Stack>
      </Box>

      {/* Log entries */}
      <Box sx={{ flex: 1, overflow: 'auto', p: 2 }}>
        {filteredLogs.length === 0 ? (
          <Alert severity="info">No logs to display</Alert>
        ) : (
          <Stack spacing={1}>
            <AnimatePresence>
              {filteredLogs.map((log) => (
                <LogEntryItem key={log.id} log={log} />
              ))}
            </AnimatePresence>
          </Stack>
        )}
      </Box>

      {dialog}
    </Paper>
  );
}

/**
 * Individual log entry component
 */
function LogEntryItem({ log }: { log: LogEntry }) {
  const [expanded, setExpanded] = useState(false);
  const hasMetadata = log.metadata && Object.keys(log.metadata).length > 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.2 }}
    >
      <Paper
        variant="outlined"
        sx={{
          p: 1.5,
          cursor: hasMetadata ? 'pointer' : 'default',
          '&:hover': hasMetadata
            ? {
                bgcolor: 'action.hover',
              }
            : undefined,
        }}
        onClick={() => hasMetadata && setExpanded(!expanded)}
      >
        <Stack spacing={1}>
          {/* Main log info */}
          <Stack direction="row" spacing={2} alignItems="flex-start">
            {/* Level badge */}
            <Chip
              label={log.level.toUpperCase()}
              size="small"
              color={getLevelColor(log.level) as any}
              sx={{ minWidth: 70 }}
            />

            {/* Component */}
            <Typography
              variant="caption"
              sx={{
                minWidth: 120,
                color: 'text.secondary',
                fontFamily: 'monospace',
              }}
            >
              {log.component}
            </Typography>

            {/* Message */}
            <Typography variant="body2" sx={{ flex: 1, wordBreak: 'break-word' }}>
              {log.message}
            </Typography>

            {/* Timestamp */}
            <Typography
              variant="caption"
              sx={{
                minWidth: 150,
                color: 'text.secondary',
                textAlign: 'right',
              }}
            >
              {formatDateTime(log.timestamp)}
            </Typography>
          </Stack>

          {/* Metadata (expandable) */}
          {hasMetadata && expanded && (
            <Box
              sx={{
                mt: 1,
                p: 1,
                bgcolor: 'grey.900',
                borderRadius: 1,
                fontFamily: 'monospace',
                fontSize: '0.75rem',
                overflow: 'auto',
                maxHeight: 200,
              }}
            >
              <pre style={{ margin: 0 }}>
                {JSON.stringify(log.metadata, null, 2)}
              </pre>
            </Box>
          )}
        </Stack>
      </Paper>
    </motion.div>
  );
}
