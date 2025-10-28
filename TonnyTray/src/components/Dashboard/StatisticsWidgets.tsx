/**
 * Statistics dashboard widgets
 * Display various metrics and analytics
 */

import { Paper, Typography, Stack, Box, CircularProgress, Tooltip } from '@mui/material';
import { motion } from 'framer-motion';
import {
  TrendingUp,
  TrendingDown,
  CheckCircle,
  Error,
  Timer,
  AccessTime,
} from '@mui/icons-material';
import { useAppStore } from '@hooks/useTauriState';
import { formatNumber, formatUptime, formatDurationMs } from '@utils/formatters';

/**
 * Animated counter component
 */
function AnimatedCounter({ value, suffix = '' }: { value: number; suffix?: string }) {
  return (
    <motion.div
      key={value}
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      <Typography variant="h3" component="div" sx={{ fontWeight: 'bold' }}>
        {formatNumber(value)}
        {suffix}
      </Typography>
    </motion.div>
  );
}

/**
 * Base statistics card component
 */
interface StatCardProps {
  title: string;
  value: number | string;
  subtitle?: string;
  icon?: React.ReactNode;
  color?: string;
  trend?: 'up' | 'down' | 'neutral';
  trendValue?: string;
  loading?: boolean;
}

function StatCard({
  title,
  value,
  subtitle,
  icon,
  color = 'primary.main',
  trend,
  trendValue,
  loading = false,
}: StatCardProps) {
  const getTrendIcon = () => {
    if (trend === 'up') return <TrendingUp fontSize="small" color="success" />;
    if (trend === 'down') return <TrendingDown fontSize="small" color="error" />;
    return null;
  };

  return (
    <Paper
      elevation={2}
      sx={{
        p: 3,
        height: '100%',
        position: 'relative',
        overflow: 'hidden',
        '&:hover': {
          boxShadow: 4,
          transform: 'translateY(-2px)',
        },
        transition: 'all 0.2s ease-in-out',
      }}
    >
      {/* Background decoration */}
      <Box
        sx={{
          position: 'absolute',
          top: -20,
          right: -20,
          width: 100,
          height: 100,
          borderRadius: '50%',
          bgcolor: color,
          opacity: 0.1,
        }}
      />

      <Stack spacing={2} sx={{ position: 'relative' }}>
        {/* Header */}
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Typography variant="subtitle2" color="text.secondary" sx={{ fontWeight: 'medium' }}>
            {title}
          </Typography>
          {icon && <Box sx={{ color }}>{icon}</Box>}
        </Stack>

        {/* Value */}
        {loading ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
            <CircularProgress size={40} />
          </Box>
        ) : (
          <Box>
            {typeof value === 'number' ? (
              <AnimatedCounter value={value} />
            ) : (
              <Typography variant="h3" component="div" sx={{ fontWeight: 'bold' }}>
                {value}
              </Typography>
            )}
          </Box>
        )}

        {/* Subtitle or trend */}
        {(subtitle || trend) && (
          <Stack direction="row" alignItems="center" spacing={1}>
            {trend && getTrendIcon()}
            {trendValue && (
              <Typography variant="caption" color="text.secondary">
                {trendValue}
              </Typography>
            )}
            {subtitle && (
              <Typography variant="body2" color="text.secondary">
                {subtitle}
              </Typography>
            )}
          </Stack>
        )}
      </Stack>
    </Paper>
  );
}

/**
 * Total commands widget
 */
export function TotalCommandsWidget() {
  const { statistics } = useAppStore();

  return (
    <StatCard
      title="Total Commands"
      value={statistics.totalCommands}
      subtitle="All time"
      icon={<AccessTime />}
      color="primary.main"
    />
  );
}

/**
 * Success rate widget
 */
export function SuccessRateWidget() {
  const { statistics } = useAppStore();

  const successRate =
    statistics.totalCommands > 0
      ? ((statistics.successfulCommands / statistics.totalCommands) * 100).toFixed(1)
      : 0;

  const getColor = () => {
    const rate = parseFloat(successRate.toString());
    if (rate >= 90) return 'success.main';
    if (rate >= 70) return 'warning.main';
    return 'error.main';
  };

  return (
    <StatCard
      title="Success Rate"
      value={`${successRate}%`}
      subtitle={`${statistics.successfulCommands} / ${statistics.totalCommands} successful`}
      icon={<CheckCircle />}
      color={getColor()}
    />
  );
}

/**
 * Failed commands widget
 */
export function FailedCommandsWidget() {
  const { statistics } = useAppStore();

  const trend =
    statistics.failedCommands === 0
      ? 'neutral'
      : statistics.failedCommands > 10
      ? 'up'
      : 'down';

  return (
    <StatCard
      title="Failed Commands"
      value={statistics.failedCommands}
      subtitle={statistics.lastError || 'No recent errors'}
      icon={<Error />}
      color="error.main"
      trend={trend}
    />
  );
}

/**
 * Average response time widget
 */
export function AverageResponseTimeWidget() {
  const { statistics } = useAppStore();

  const responseTime = statistics.averageResponseTime;
  const formatted = formatDurationMs(responseTime);

  const getColor = () => {
    if (responseTime < 1000) return 'success.main';
    if (responseTime < 3000) return 'warning.main';
    return 'error.main';
  };

  return (
    <StatCard
      title="Avg Response Time"
      value={formatted}
      subtitle="End-to-end latency"
      icon={<Timer />}
      color={getColor()}
    />
  );
}

/**
 * Uptime widget
 */
export function UptimeWidget() {
  const { statistics } = useAppStore();

  const uptimeFormatted = formatUptime(statistics.uptime);

  return (
    <StatCard
      title="Uptime"
      value={uptimeFormatted}
      subtitle="Since last restart"
      icon={<AccessTime />}
      color="info.main"
    />
  );
}

/**
 * Success/Failure breakdown widget with circular progress
 */
export function SuccessBreakdownWidget() {
  const { statistics } = useAppStore();

  const successPercentage =
    statistics.totalCommands > 0
      ? (statistics.successfulCommands / statistics.totalCommands) * 100
      : 0;

  const getColor = () => {
    if (successPercentage >= 90) return 'success';
    if (successPercentage >= 70) return 'warning';
    return 'error';
  };

  return (
    <Paper
      elevation={2}
      sx={{
        p: 3,
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 2 }}>
        Command Success Breakdown
      </Typography>

      <Box sx={{ position: 'relative', display: 'inline-flex' }}>
        <CircularProgress
          variant="determinate"
          value={successPercentage}
          size={120}
          thickness={6}
          color={getColor()}
        />
        <Box
          sx={{
            top: 0,
            left: 0,
            bottom: 0,
            right: 0,
            position: 'absolute',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexDirection: 'column',
          }}
        >
          <Typography variant="h4" component="div" color="text.primary">
            {successPercentage.toFixed(0)}%
          </Typography>
        </Box>
      </Box>

      <Stack direction="row" spacing={3} sx={{ mt: 3 }}>
        <Tooltip title="Successful commands">
          <Box sx={{ textAlign: 'center' }}>
            <Typography variant="h6" color="success.main">
              {statistics.successfulCommands}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Success
            </Typography>
          </Box>
        </Tooltip>

        <Tooltip title="Failed commands">
          <Box sx={{ textAlign: 'center' }}>
            <Typography variant="h6" color="error.main">
              {statistics.failedCommands}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Failed
            </Typography>
          </Box>
        </Tooltip>
      </Stack>
    </Paper>
  );
}

/**
 * All statistics widgets in a grid
 */
export function StatisticsGrid() {
  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: 3 }}>
      <TotalCommandsWidget />
      <SuccessRateWidget />
      <AverageResponseTimeWidget />
      <UptimeWidget />
      <FailedCommandsWidget />
      <Box sx={{ gridColumn: 'span 2' }}>
        <SuccessBreakdownWidget />
      </Box>
    </Box>
  );
}
