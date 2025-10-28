/**
 * Central export for all components
 * Makes imports cleaner throughout the application
 */

// Common components
export { ErrorBoundary } from './Common/ErrorBoundary';
export { default as ConfirmDialog, useConfirmDialog } from './Common/ConfirmDialog';
export { default as LoadingScreen } from './Common/LoadingScreen';
export { default as ErrorScreen } from './Common/ErrorScreen';
export * from './Common/Skeletons';

// Audio components
export { default as AudioLevelMeter, CompactAudioLevelMeter } from './Audio/AudioLevelMeter';

// Dashboard components
export { default as Dashboard } from './Dashboard/Dashboard';
export { default as Header } from './Dashboard/Header';
export { default as RecordingControls } from './Dashboard/RecordingControls';
export { default as StatusPanel } from './Dashboard/StatusPanel';
export { default as TranscriptionPanel } from './Dashboard/TranscriptionPanel';
export { default as QuickActions } from './Dashboard/QuickActions';
export * from './Dashboard/StatisticsWidgets';

// Settings components
export { default as Settings } from './Settings/Settings';
export { default as VoiceConfigTab } from './Settings/VoiceConfigTab';
export { default as IntegrationTab } from './Settings/IntegrationTab';
export { default as ServerConfigTab } from './Settings/ServerConfigTab';
export { default as AdvancedTab } from './Settings/AdvancedTab';
export { default as KeyboardShortcutPicker, KeyboardShortcutsManager } from './Settings/KeyboardShortcutPicker';
export { default as SettingsManager } from './Settings/SettingsManager';

// Profile components
export { default as ProfileSelector } from './Profile/ProfileSelector';

// Logs components
export { default as LogsViewer } from './Logs/LogsViewer';
