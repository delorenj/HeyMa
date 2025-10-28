/**
 * Enhanced error boundary with recovery and detailed error display
 */

import React, { Component, ReactNode } from 'react';
import {
  Box,
  Button,
  Container,
  Paper,
  Typography,
  Stack,
  Collapse,
  Alert,
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import BugReportIcon from '@mui/icons-material/BugReport';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ExpandLessIcon from '@mui/icons-material/ExpandLess';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  onError?: (error: Error, errorInfo: React.ErrorInfo) => void;
}

interface State {
  hasError: boolean;
  error: Error | null;
  errorInfo: React.ErrorInfo | null;
  showDetails: boolean;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = {
      hasError: false,
      error: null,
      errorInfo: null,
      showDetails: false,
    };
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return {
      hasError: true,
      error,
    };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    this.setState({ errorInfo });

    // Log to console
    console.error('ErrorBoundary caught an error:', error, errorInfo);

    // Call optional error handler
    this.props.onError?.(error, errorInfo);
  }

  handleReset = (): void => {
    this.setState({
      hasError: false,
      error: null,
      errorInfo: null,
      showDetails: false,
    });
  };

  handleReload = (): void => {
    window.location.reload();
  };

  toggleDetails = (): void => {
    this.setState((prev) => ({ showDetails: !prev.showDetails }));
  };

  render(): ReactNode {
    const { hasError, error, errorInfo, showDetails } = this.state;
    const { children, fallback } = this.props;

    if (!hasError) {
      return children;
    }

    if (fallback) {
      return fallback;
    }

    return (
      <Box
        sx={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          bgcolor: 'background.default',
          p: 3,
        }}
      >
        <Container maxWidth="md">
          <Paper
            elevation={3}
            sx={{
              p: 4,
              borderTop: 4,
              borderColor: 'error.main',
            }}
          >
            <Stack spacing={3}>
              {/* Icon and Title */}
              <Box sx={{ textAlign: 'center' }}>
                <BugReportIcon
                  sx={{
                    fontSize: 64,
                    color: 'error.main',
                    mb: 2,
                  }}
                />
                <Typography variant="h4" gutterBottom>
                  Something Went Wrong
                </Typography>
                <Typography variant="body1" color="text.secondary">
                  The application encountered an unexpected error. You can try to recover or reload
                  the page.
                </Typography>
              </Box>

              {/* Error Message */}
              {error && (
                <Alert severity="error" sx={{ textAlign: 'left' }}>
                  <Typography variant="subtitle2" gutterBottom>
                    Error Message:
                  </Typography>
                  <Typography variant="body2" component="pre" sx={{ fontFamily: 'monospace' }}>
                    {error.toString()}
                  </Typography>
                </Alert>
              )}

              {/* Action Buttons */}
              <Stack direction="row" spacing={2} justifyContent="center">
                <Button
                  variant="contained"
                  startIcon={<RefreshIcon />}
                  onClick={this.handleReset}
                  color="primary"
                >
                  Try Again
                </Button>
                <Button variant="outlined" onClick={this.handleReload} color="secondary">
                  Reload Page
                </Button>
              </Stack>

              {/* Error Details (Collapsible) */}
              {errorInfo && (
                <Box>
                  <Button
                    fullWidth
                    onClick={this.toggleDetails}
                    endIcon={showDetails ? <ExpandLessIcon /> : <ExpandMoreIcon />}
                    sx={{ justifyContent: 'space-between' }}
                  >
                    <Typography variant="button">Technical Details</Typography>
                  </Button>

                  <Collapse in={showDetails}>
                    <Paper
                      variant="outlined"
                      sx={{
                        mt: 2,
                        p: 2,
                        bgcolor: 'grey.900',
                        maxHeight: 400,
                        overflow: 'auto',
                      }}
                    >
                      <Typography
                        variant="body2"
                        component="pre"
                        sx={{
                          fontFamily: 'monospace',
                          fontSize: '0.75rem',
                          color: 'grey.300',
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                        }}
                      >
                        {errorInfo.componentStack}
                      </Typography>
                    </Paper>
                  </Collapse>
                </Box>
              )}

              {/* Help Text */}
              <Typography variant="caption" color="text.secondary" sx={{ textAlign: 'center' }}>
                If this problem persists, please check the logs for more information or restart the
                application.
              </Typography>
            </Stack>
          </Paper>
        </Container>
      </Box>
    );
  }
}

export default ErrorBoundary;
