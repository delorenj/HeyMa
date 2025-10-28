/**
 * Dashboard header component
 * Shows app title and navigation controls
 */

import { AppBar, Toolbar, Typography, IconButton, Box } from '@mui/material';
import SettingsIcon from '@mui/icons-material/Settings';
import MicIcon from '@mui/icons-material/Mic';

interface HeaderProps {
  onSettingsClick: () => void;
}

export default function Header({ onSettingsClick }: HeaderProps) {
  return (
    <AppBar position="sticky" elevation={0} sx={{ bgcolor: 'background.paper' }}>
      <Toolbar>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <MicIcon sx={{ color: 'primary.main', fontSize: 28 }} />
          <Typography variant="h6" component="h1" sx={{ color: 'text.primary' }}>
            TonnyTray
          </Typography>
        </Box>

        <Box sx={{ flexGrow: 1 }} />

        <IconButton
          onClick={onSettingsClick}
          sx={{ color: 'text.primary' }}
          aria-label="Settings"
        >
          <SettingsIcon />
        </IconButton>
      </Toolbar>
    </AppBar>
  );
}
