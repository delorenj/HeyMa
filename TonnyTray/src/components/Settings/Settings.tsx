/**
 * Main settings component
 * Tabbed interface for all configuration options
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Box,
  Container,
  Paper,
  Tabs,
  Tab,
  AppBar,
  Toolbar,
  Typography,
  IconButton,
} from '@mui/material';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import MicIcon from '@mui/icons-material/Mic';
import IntegrationInstructionsIcon from '@mui/icons-material/IntegrationInstructions';
import StorageIcon from '@mui/icons-material/Storage';
import TuneIcon from '@mui/icons-material/Tune';

import VoiceConfigTab from './VoiceConfigTab';
import IntegrationTab from './IntegrationTab';
import ServerConfigTab from './ServerConfigTab';
import AdvancedTab from './AdvancedTab';

interface TabPanelProps {
  children?: React.ReactNode;
  index: number;
  value: number;
}

function TabPanel(props: TabPanelProps) {
  const { children, value, index, ...other } = props;

  return (
    <div
      role="tabpanel"
      hidden={value !== index}
      id={`settings-tabpanel-${index}`}
      aria-labelledby={`settings-tab-${index}`}
      {...other}
    >
      {value === index && <Box sx={{ py: 3 }}>{children}</Box>}
    </div>
  );
}

export default function Settings() {
  const navigate = useNavigate();
  const [tabValue, setTabValue] = useState(0);

  const handleTabChange = (_event: React.SyntheticEvent, newValue: number) => {
    setTabValue(newValue);
  };

  return (
    <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
      <AppBar position="sticky" elevation={0} sx={{ bgcolor: 'background.paper' }}>
        <Toolbar>
          <IconButton
            edge="start"
            onClick={() => navigate('/')}
            sx={{ mr: 2, color: 'text.primary' }}
          >
            <ArrowBackIcon />
          </IconButton>
          <Typography variant="h6" component="h1" sx={{ color: 'text.primary' }}>
            Settings
          </Typography>
        </Toolbar>
      </AppBar>

      <Container maxWidth="lg" sx={{ py: 4 }}>
        <Paper>
          <Tabs
            value={tabValue}
            onChange={handleTabChange}
            variant="scrollable"
            scrollButtons="auto"
            sx={{ borderBottom: 1, borderColor: 'divider' }}
          >
            <Tab
              icon={<MicIcon />}
              label="Voice Configuration"
              id="settings-tab-0"
            />
            <Tab
              icon={<IntegrationInstructionsIcon />}
              label="Integration Settings"
              id="settings-tab-1"
            />
            <Tab
              icon={<StorageIcon />}
              label="Server Configuration"
              id="settings-tab-2"
            />
            <Tab icon={<TuneIcon />} label="Advanced Settings" id="settings-tab-3" />
          </Tabs>

          <Box sx={{ px: 3 }}>
            <TabPanel value={tabValue} index={0}>
              <VoiceConfigTab />
            </TabPanel>
            <TabPanel value={tabValue} index={1}>
              <IntegrationTab />
            </TabPanel>
            <TabPanel value={tabValue} index={2}>
              <ServerConfigTab />
            </TabPanel>
            <TabPanel value={tabValue} index={3}>
              <AdvancedTab />
            </TabPanel>
          </Box>
        </Paper>
      </Container>
    </Box>
  );
}
