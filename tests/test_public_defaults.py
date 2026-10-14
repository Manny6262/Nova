"""Public-template privacy defaults and configuration do not inherit a personal grant."""
from pathlib import Path
import json
import unittest
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]


class PublicDefaults(unittest.TestCase):
    def test_native_optional_capabilities_start_off(self):
        tree=ET.parse(ROOT/'NovaNative/MainPage.xaml')
        names={'ToolsToggle','ActionToggle','ComputerToggle'}
        found={e.attrib.get('{http://schemas.microsoft.com/winfx/2006/xaml}Name'):e.attrib.get('IsChecked') for e in tree.iter()}
        self.assertTrue(all(found.get(name)=='False' for name in names))

    def test_browser_personal_full_control_grant_removed(self):
        text=(ROOT/'engine/templates/index.html').read_text(encoding='utf-8')
        self.assertNotIn('novaFullControlGrant20260906',text)
        self.assertIn('let fullControlEnabled = false;',text)

    def test_example_config_has_no_live_policy_or_cloud_provider(self):
        settings=json.loads((ROOT/'engine/nova_settings.example.json').read_text())
        policy=json.loads((ROOT/'engine/.nova_mcp_policy.example.json').read_text())
        self.assertEqual(settings['provider'],'local')
        self.assertEqual(policy,{'token':'','expires':0})

    def test_installation_migration_is_an_explicit_choice(self):
        text=(ROOT/'Install Nova Native.ps1').read_text(encoding='utf-8')
        self.assertIn("[string]$MigrateFrom = ''",text)
        self.assertIn('__no_migration_requested__',text)


if __name__=='__main__':unittest.main(verbosity=2)
