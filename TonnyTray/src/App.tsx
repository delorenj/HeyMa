/**
 * Main application component
 * Handles routing, theming, and global state initialization
 */

import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { ThemeProvider, CssBaseline } from '@mui/material';
import { SnackbarProvider } from 'notistack';
import { useTauriState } from '@hooks/useTauriState';
import { useAppTheme } from '@theme/index';
import { ErrorBoundary } from '@components/Common/ErrorBoundary';
import Dashboard from '@components/Dashboard/Dashboard';
import Settings from '@components/Settings/Settings';
import LoadingScreen from '@components/Common/LoadingScreen';
import ErrorScreen from '@components/Common/ErrorScreen';

/**
 * Main App component
 */
function App() {
  const { theme } = useAppTheme();
  const { isInitialized, error, retry } = useTauriState();

  // Show loading screen while initializing
  if (!isInitialized && !error) {
    return (
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <LoadingScreen message="Initializing TonnyTray..." />
      </ThemeProvider>
    );
  }

  // Show error screen if initialization failed
  if (error) {
    return (
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <ErrorScreen error={error} onRetry={retry} />
      </ThemeProvider>
    );
  }

  return (
    <ErrorBoundary>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <SnackbarProvider
          maxSnack={3}
          anchorOrigin={{
            vertical: 'bottom',
            horizontal: 'right',
          }}
          autoHideDuration={5000}
        >
          <Router>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Router>
        </SnackbarProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

export default App;
