*** Settings ***
Resource        ../../resource/rnavn_a_ivi2_CDC_factor.resource
Test Setup    Test-Precondition_RNAVN
Test Teardown    Test-Postcondition    ${TEST STATUS}

*** Variables ***
${CPU_INDEX}        0
${value}            tmp
${alive}            tmp
${i}                0
${testCount}        10000
${start_ms}      10
${step_ms}       10
${max_ms}        15000
${interval_ms}    10
${FORCE_DOCKER_MODE}    False

*** Test Cases ***
Reliability_RVC_AVM
    Log With Time    - Test Start ${TEST NAME} -
    FOR    ${current_count}    IN RANGE    ${testCount}
        ${start_time}=    Get Current Date    result_format=%Y-%m-%d %H:%M:%S.%f
        Log With Time   [CYCLE] index=${current_count + 1} total=${testCount} start=${start_time}
        QNX Connect
        MCU Connect
        SAIL Connect
        Wait    1
        Run Keyword And Ignore Error    Save Meminfo Log    ${AVN_AND_1}    ${TEST NAME}
        Wait    1
        ${status}    ${log_path}=    Run Keyword And Ignore Error     Save Android Log    ${AVN_AND_1}    ${TEST NAME}
        Wait    5
        Arduino Connect
        Wait    2
        Set Panel To Black
        Wait    2
        RVC_Performance    1260    120    ${step_ms}
        Wait    2
        Arduino Disconnect
        Run Keyword And Ignore Error    ANDROID_EXT.Stop Logcat
        Run Keyword And Ignore Error    Save RBVM Android Log    ${AVN_AND_2}    ${TEST NAME}
        Wait    10
        Run Keyword And Ignore Error    Save Dropbox Log    ${AVN_AND_1}    ${TEST NAME}
        Wait    10
        Run Keyword And Ignore Error    Save Bugreport    ${AVN_AND_1}    ${TEST NAME}
        Wait    90
        Run Keyword And Ignore Error    Save Dumpsys Log    ${AVN_AND_2}    ${TEST NAME}
        Wait    10
        Run Keyword And Ignore Error    Save Dmesg Log        ${AVN_AND_1}    ${TEST NAME}
        Wait    10
        QNX Disconnect
        MCU Disconnect
        SAIL Disconnect

        ${step_ms}=    Evaluate        ${step_ms} + ${interval_ms}
        IF    ${step_ms} > ${max_ms}
            ${step_ms}=    Set Variable    ${start_ms}
        END

    END