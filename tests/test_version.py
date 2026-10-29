import unitelabs.opentrons_flex


def test_package_has_version():
    assert unitelabs.opentrons_flex.__version__ is not None


def test_sila_server_version_is_the_package_version_in_sila_form():
    from unitelabs.opentrons_flex import _sila_server_version

    assert _sila_server_version("0.9.1") == "0.9.1"
    assert _sila_server_version("0.9.1+asms.1") == "0.9.1_asms_1"
