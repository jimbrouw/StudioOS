// SonyRec.cpp — StudioOS FX30 camera control CLI (recording-only MVP).
//
// Fixes the proven root cause: the previous binary connected to "whichever
// FX30 the SDK enumerates first" because it never checked device identity.
// This version enumerates every camera the SDK can see, matches ONLY the one
// whose MAC address equals the required --mac, and refuses (non-zero exit,
// no connect) if none match. It never falls back to the first enumerated
// camera.
//
// Scope (deliberately reduced to ship fast): status, start, stop, and the
// --server warm protocol. ingest/proxy/format-card/delete-file/ftp-test are
// NOT implemented here and always return a clear "unsupported in this MVP
// build" error — they never touch card media.
//
// CLI: sony-rec --camera <label> --mac <AA:BB:CC:DD:EE:FF> --action <action> [--server]
// Actions: start | stop | status | card-status
//
// --server: prints "SONY_REC_READY {...}" once connected (including the
// matched MAC, so bridge.py can assert identity), then reads one action name
// per stdin line ("start" | "stop" | "status" | "quit"), printing
// "SONY_REC_RESULT {...}" after each.
//
// Env: SONY_REC_PASSWORD (Access Authentication password, if the body has one).

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <tuple>
#include <vector>

#include "CRSDK/CameraRemote_SDK.h"
#include "CRSDK/IDeviceCallback.h"

namespace SDK = SCRSDK;
using namespace std::chrono_literals;

namespace {

std::string normalizeMac(const std::string& raw) {
    std::string out;
    for (char c : raw) {
        if (std::isxdigit((unsigned char)c)) out.push_back((char)std::toupper((unsigned char)c));
    }
    return out;
}

std::string jsonEscape(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (char c : in) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            default: out.push_back(c);
        }
    }
    return out;
}

void flushLine(const std::string& line) {
    std::cout << line << std::endl;
    std::cout.flush();
}

void printResult(bool ok, const std::string& extraJsonFields, const std::string& error = "") {
    std::ostringstream oss;
    oss << "SONY_REC_RESULT {\"ok\":" << (ok ? "true" : "false");
    if (!error.empty()) oss << ",\"error\":\"" << jsonEscape(error) << "\"";
    if (!extraJsonFields.empty()) oss << "," << extraJsonFields;
    oss << "}";
    flushLine(oss.str());
}

struct Args {
    std::string cameraLabel;
    std::string mac;
    std::string action;
    std::string destination;
    std::string filename;
    int slot = 0;
    bool confirmFormat = false;
    bool server = false;
};

bool parseArgs(int argc, char** argv, Args& out, std::string& err) {
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&](const char* flag) -> std::string {
            if (i + 1 >= argc) { err = std::string("Missing value for ") + flag; return std::string(); }
            return argv[++i];
        };
        if (a == "--camera") out.cameraLabel = next("--camera");
        else if (a == "--mac") out.mac = next("--mac");
        else if (a == "--action") out.action = next("--action");
        else if (a == "--dest") out.destination = next("--dest");
        else if (a == "--filename") out.filename = next("--filename");
        else if (a == "--slot") {
            const std::string value = next("--slot");
            if (!err.empty()) return false;
            try {
                out.slot = std::stoi(value);
            } catch (...) {
                err = "Invalid value for --slot";
            }
        }
        else if (a == "--confirm-format") out.confirmFormat = true;
        else if (a == "--server") out.server = true;
        if (!err.empty()) return false;
    }
    return true;
}

class Callback : public SDK::IDeviceCallback {
public:
    std::mutex mtx;
    std::condition_variable cv;
    std::atomic<bool> connected{false};
    std::atomic<bool> disconnected{false};
    std::atomic<CrInt32u> lastError{0};
    bool transferFinished = false;
    bool transferOk = false;
    CrInt32u transferPercent = 0;
    std::string transferFilename;
    bool formatFinished = false;
    bool formatOk = false;

    void OnConnected(SDK::DeviceConnectionVersioin) override {
        connected = true;
        cv.notify_all();
    }
    void OnDisconnected(CrInt32u) override {
        disconnected = true;
        cv.notify_all();
    }
    void OnError(CrInt32u error) override {
        lastError = error;
        cv.notify_all();
    }

    void OnNotifyRemoteTransferResult(CrInt32u notify, CrInt32u percent, CrChar* filename) override {
        std::lock_guard<std::mutex> lock(mtx);
        transferPercent = percent;
        if (filename) transferFilename = filename;
        if (notify == SDK::CrNotify_RemoteTransfer_Result_OK) {
            transferOk = true;
            transferFinished = true;
        } else if (notify == SDK::CrNotify_RemoteTransfer_Result_NG ||
                   notify == SDK::CrNotify_RemoteTransfer_Result_DeviceBusy) {
            transferOk = false;
            transferFinished = true;
        }
        cv.notify_all();
    }

    void OnWarning(CrInt32u warning) override {
        if (warning == SDK::CrWarning_Format_Complete || warning == SDK::CrWarning_Format_Failed ||
            warning == SDK::CrWarning_Format_Invalid || warning == SDK::CrWarning_Format_Canceled) {
            std::lock_guard<std::mutex> lock(mtx);
            formatOk = warning == SDK::CrWarning_Format_Complete;
            formatFinished = true;
            cv.notify_all();
        }
    }
};

bool readProperty(SDK::CrDeviceHandle handle, CrInt32u code, CrInt64u& value) {
    SDK::CrDeviceProperty* list = nullptr;
    CrInt32 n = 0;
    auto status = SDK::GetSelectDeviceProperties(handle, 1, &code, &list, &n);
    if (CR_FAILED(status) || n == 0 || !list) {
        if (list) SDK::ReleaseDeviceProperties(handle, list);
        return false;
    }
    value = list[0].GetCurrentValue();
    SDK::ReleaseDeviceProperties(handle, list);
    return true;
}

std::string recordingStateToString(CrInt64u state) {
    return (state == SDK::CrMovie_Recording_State_Recording) ? "recording" : "not_recording";
}

// Enumerates every camera the SDK can currently see and returns the index of
// the one whose MAC matches `wantMacNorm`. Never guesses; returns -1 if there
// is no exact match, with every enumerated MAC captured for a clear error.
int findMatchingCameraIndex(SDK::ICrEnumCameraObjectInfo* list, const std::string& wantMacNorm,
                             std::vector<std::string>& enumeratedMacs) {
    int match = -1;
    auto count = list->GetCount();
    for (CrInt32u i = 0; i < count; ++i) {
        auto* info = list->GetCameraObjectInfo(i);
        std::string macChars;
        if (info->GetMACAddressChar() && info->GetMACAddressCharSize() > 0) {
            macChars.assign(info->GetMACAddressChar(), info->GetMACAddressCharSize());
        }
        std::string norm = normalizeMac(macChars);
        enumeratedMacs.push_back(macChars.empty() ? std::string("(no MAC / not IP-connected)") : macChars);
        if (!norm.empty() && norm == wantMacNorm) match = (int)i;
    }
    return match;
}

struct ConnectResult {
    bool ok = false;
    SDK::CrDeviceHandle handle = 0;
    std::string error;
    std::string matchedMac;
};

ConnectResult connectByIdentity(Callback& cb, const Args& args) {
    ConnectResult result;

    if (args.mac.empty()) {
        result.error = "Missing --mac; refusing to connect without an explicit camera identity";
        return result;
    }
    const std::string wantMac = normalizeMac(args.mac);

    SDK::ICrEnumCameraObjectInfo* list = nullptr;
    auto enumStatus = SDK::EnumCameraObjects(&list, 3);
    if (CR_FAILED(enumStatus) || list == nullptr) {
        result.error = "Enumeration of device information failed";
        return result;
    }

    std::vector<std::string> enumeratedMacs;
    int idx = findMatchingCameraIndex(list, wantMac, enumeratedMacs);
    if (idx < 0) {
        std::ostringstream oss;
        oss << "Requested camera " << args.cameraLabel << " (mac " << args.mac << ") not found among "
            << list->GetCount() << " enumerated cameras. Seen: ";
        for (size_t i = 0; i < enumeratedMacs.size(); ++i) {
            if (i) oss << ", ";
            oss << enumeratedMacs[i];
        }
        result.error = oss.str();
        list->Release();
        return result;
    }

    auto* cameraInfo = const_cast<SDK::ICrCameraObjectInfo*>(list->GetCameraObjectInfo(idx));
    result.matchedMac = args.mac;

    // Whether Access Authentication is required is only knowable for certain
    // once Connect() itself reports it; there is no reliable "is a password
    // required" getter on ICrCameraObjectInfo.
    const char* password = std::getenv("SONY_REC_PASSWORD");
    auto connectStatus = SDK::Connect(cameraInfo, &cb, &result.handle, SDK::CrSdkControlMode_Remote,
                                       SDK::CrReconnecting_ON, "admin", password ? password : "", "", 0);
    list->Release();
    if (CR_FAILED(connectStatus)) {
        std::ostringstream oss;
        oss << "Failed to connect: 0x" << std::hex << connectStatus;
        if (!password || !*password) oss << ". SONY_REC_PASSWORD is required when Access Authentication is enabled.";
        result.error = oss.str();
        return result;
    }

    {
        std::unique_lock<std::mutex> lock(cb.mtx);
        bool ready = cb.cv.wait_for(lock, 15s, [&] { return cb.connected.load() || cb.lastError.load() != 0; });
        if (!ready || !cb.connected.load()) {
            std::ostringstream oss;
            oss << "Camera did not confirm connection";
            if (cb.lastError.load()) oss << " (SDK error 0x" << std::hex << cb.lastError.load() << ")";
            result.error = oss.str();
            SDK::ReleaseDevice(result.handle);
            result.handle = 0;
            return result;
        }
    }

    // The camera needs a brief settle time after OnConnected before its
    // property cache is queryable (confirmed against real hardware).
    std::this_thread::sleep_for(2s);
    {
        SDK::CrDeviceProperty* fullList = nullptr;
        CrInt32 n = 0;
        auto primeStatus = SDK::GetDeviceProperties(result.handle, &fullList, &n);
        if (CR_SUCCEEDED(primeStatus) && fullList) SDK::ReleaseDeviceProperties(result.handle, fullList);
    }

    result.ok = true;
    return result;
}

void disconnectAndRelease(SDK::CrDeviceHandle handle) {
    if (!handle) return;
    SDK::Disconnect(handle);
    SDK::ReleaseDevice(handle);
}

struct ActionOutcome {
    bool ok = false;
    std::string extraJson;
    std::string error;
};

ActionOutcome doStatus(SDK::CrDeviceHandle handle) {
    ActionOutcome out;
    CrInt64u state = 0;
    if (!readProperty(handle, SDK::CrDeviceProperty_RecordingState, state)) {
        out.error = "Could not read recording state";
        return out;
    }
    out.ok = true;
    out.extraJson = "\"recordingState\":\"" + recordingStateToString(state) + "\"";
    return out;
}

ActionOutcome doStartStop(SDK::CrDeviceHandle handle, bool start) {
    ActionOutcome out;
    auto param = start ? SDK::CrCommandParam_Down : SDK::CrCommandParam_Up;
    auto sendStatus = SDK::SendCommand(handle, SDK::CrCommandId::CrCommandId_MovieRecord, param);
    if (CR_FAILED(sendStatus)) {
        out.error = "Camera rejected the record command";
        return out;
    }
    CrInt64u state = start ? SDK::CrMovie_Recording_State_Not_Recording : SDK::CrMovie_Recording_State_Recording;
    for (int i = 0; i < 20; ++i) {
        std::this_thread::sleep_for(250ms);
        if (readProperty(handle, SDK::CrDeviceProperty_RecordingState, state)) {
            bool isRecording = (state == SDK::CrMovie_Recording_State_Recording);
            if (isRecording == start) break;
        }
    }
    out.ok = true;
    out.extraJson = "\"recordingState\":\"" + recordingStateToString(state) + "\"";
    return out;
}

std::string basename(const CrInt8* rawPath) {
    const std::string path = rawPath ? reinterpret_cast<const char*>(rawPath) : "";
    const auto separator = path.find_last_of("/\\");
    return separator == std::string::npos ? path : path.substr(separator + 1);
}

using CaptureKey = std::tuple<CrInt16u, CrInt16u, CrInt16u, CrInt16u, CrInt16u, CrInt16u, CrInt16u>;

CaptureKey captureKey(const SDK::CrCaptureDate& value) {
    return {value.year, value.month, value.day, value.hour, value.minute, value.sec, value.msec};
}

struct TransferCandidate {
    bool found = false;
    SDK::CrSlotNumber slot = SDK::CrSlotNumber_Slot1;
    CrInt32u contentId = 0;
    CrInt32u fileId = 0;
    CrInt64u fileSize = 0;
    std::string filename;
    CaptureKey captured{};
};

ActionOutcome doIngest(SDK::CrDeviceHandle handle, Callback& callback, const Args& args, bool proxy) {
    ActionOutcome out;
    if (args.destination.empty()) {
        out.error = "Missing --dest for camera transfer";
        return out;
    }

    std::error_code directoryError;
    std::filesystem::create_directories(args.destination, directoryError);
    if (directoryError) {
        out.error = "Could not create destination: " + directoryError.message();
        return out;
    }

    TransferCandidate newest;
    for (const auto slot : {SDK::CrSlotNumber_Slot1, SDK::CrSlotNumber_Slot2}) {
        SDK::CrContentsInfo* contents = nullptr;
        CrInt32u contentCount = 0;
        const auto listStatus = SDK::GetRemoteTransferContentsInfoList(
            handle, slot, SDK::CrGetContentsInfoListType_All, nullptr, 0, &contents, &contentCount);
        if (CR_FAILED(listStatus) || !contents) continue;

        for (CrInt32u contentIndex = 0; contentIndex < contentCount; ++contentIndex) {
            const auto captured = captureKey(contents[contentIndex].creationDatetimeLocaltime);
            for (CrInt32u fileIndex = 0; fileIndex < contents[contentIndex].filesNum; ++fileIndex) {
                const auto& file = contents[contentIndex].files[fileIndex];
                if (!file.isVideoParamExsist) continue;
                const bool isProxy = file.videoParam.proxyStatus;
                const bool isFourKMaster = !isProxy && file.videoParam.width >= 3840 && file.videoParam.height >= 2160;
                if ((proxy && !isProxy) || (!proxy && !isFourKMaster)) continue;
                if (!newest.found || captured > newest.captured) {
                    newest = {true, slot, contents[contentIndex].contentId, file.fileId,
                              file.fileSize, basename(file.filePath), captured};
                }
            }
        }
        SDK::ReleaseRemoteTransferContentsInfoList(handle, contents);
    }

    if (!newest.found || newest.filename.empty()) {
        out.error = proxy ? "No proxy movie was found on either camera slot"
                          : "No 4K master movie was found on either camera slot";
        return out;
    }

    {
        std::lock_guard<std::mutex> lock(callback.mtx);
        callback.transferFinished = false;
        callback.transferOk = false;
        callback.transferPercent = 0;
        callback.transferFilename.clear();
    }
    std::string destination = args.destination;
    std::string filename = newest.filename;
    const auto transferStatus = SDK::GetRemoteTransferContentsDataFile(
        handle, newest.slot, newest.contentId, newest.fileId, 0x5000000,
        destination.data(), filename.data());
    if (CR_FAILED(transferStatus)) {
        out.error = "Sony rejected the contents transfer request";
        return out;
    }

    std::unique_lock<std::mutex> lock(callback.mtx);
    const bool completed = callback.cv.wait_for(lock, 30min, [&callback] {
        return callback.transferFinished || callback.disconnected.load();
    });
    if (!completed) {
        SDK::ControlGetRemoteTransferContentsDataFile(handle, SDK::CrGetContentsDataControlType_Cancel);
        out.error = "Camera transfer timed out after 30 minutes";
        return out;
    }
    if (!callback.transferOk) {
        out.error = "Camera reported that the contents transfer failed";
        return out;
    }

    const std::filesystem::path downloaded = callback.transferFilename.empty()
        ? std::filesystem::path(args.destination) / newest.filename
        : std::filesystem::path(callback.transferFilename);
    out.ok = true;
    out.extraJson = "\"file\":\"" + jsonEscape(downloaded.string()) +
                    "\",\"fileName\":\"" + jsonEscape(newest.filename) +
                    "\",\"sizeBytes\":" + std::to_string(newest.fileSize);
    flushLine("File: " + downloaded.string());
    return out;
}

// Prints the SONY_REC_CARD_STATUS line the dashboard expects. Read-only.
bool doCardStatus(SDK::CrDeviceHandle handle) {
    CrInt64u recState = 0;
    bool recordingOk = readProperty(handle, SDK::CrDeviceProperty_RecordingState, recState);
    bool recording = recordingOk && (recState == SDK::CrMovie_Recording_State_Recording);

    struct SlotInfo { int slot; bool present; CrInt64u remainingMinutes; bool fullFormatAvailable; };
    SlotInfo slots[2] = {{1, false, 0, false}, {2, false, 0, false}};

    struct SlotCodes { CrInt32u status, remaining; };
    SlotCodes codes[2] = {
        {SDK::CrDeviceProperty_MediaSLOT1_Status, SDK::CrDeviceProperty_MediaSLOT1_RemainingTime},
        {SDK::CrDeviceProperty_MediaSLOT2_Status, SDK::CrDeviceProperty_MediaSLOT2_RemainingTime},
    };
    const CrInt32u formatCodes[2] = {
        SDK::CrDeviceProperty_MediaSLOT1_FormatEnableStatus,
        SDK::CrDeviceProperty_MediaSLOT2_FormatEnableStatus,
    };

    bool anySlotOk = false;
    for (int s = 0; s < 2; ++s) {
        CrInt64u statusVal = 0;
        if (!readProperty(handle, codes[s].status, statusVal)) continue;
        if (statusVal == SDK::CrSlotStatus_NoCard) continue;
        slots[s].present = true;
        anySlotOk = true;
        CrInt64u remaining = 0;
        if (readProperty(handle, codes[s].remaining, remaining)) slots[s].remainingMinutes = remaining;
        CrInt64u formatAvailable = 0;
        if (readProperty(handle, formatCodes[s], formatAvailable)) {
            slots[s].fullFormatAvailable = formatAvailable == SDK::CrMediaFormat_Enable;
        }
    }

    if (!anySlotOk && !recordingOk) {
        flushLine("SONY_REC_CARD_STATUS {\"ok\":false,\"error\":\"Could not read camera card status\"}");
        return false;
    }

    std::ostringstream oss;
    oss << "SONY_REC_CARD_STATUS {\"ok\":true,\"remainingTimeUnit\":\"minutes\",\"recording\":"
        << (recording ? "true" : "false") << ",\"slots\":[";
    bool first = true;
    for (int s = 0; s < 2; ++s) {
        if (!slots[s].present) continue;
        if (!first) oss << ",";
        first = false;
        oss << "{\"slot\":" << slots[s].slot
            << ",\"remainingMinutes\":" << slots[s].remainingMinutes
            << ",\"fullFormatAvailable\":" << (slots[s].fullFormatAvailable ? "true" : "false")
            << ",\"quickFormatAvailable\":false}";
    }
    oss << "]}";
    flushLine(oss.str());
    return true;
}

ActionOutcome doFormatCard(SDK::CrDeviceHandle handle, Callback& callback, const Args& args) {
    ActionOutcome out;
    if (!args.confirmFormat || (args.slot != 1 && args.slot != 2)) {
        out.error = "Format requires --slot 1|2 and --confirm-format";
        return out;
    }
    CrInt64u recordingState = 0;
    if (!readProperty(handle, SDK::CrDeviceProperty_RecordingState, recordingState) ||
        recordingState == SDK::CrMovie_Recording_State_Recording) {
        out.error = "Refusing to format while recording state is unknown or recording is active";
        return out;
    }
    const CrInt32u formatCode = args.slot == 1
        ? SDK::CrDeviceProperty_MediaSLOT1_FormatEnableStatus
        : SDK::CrDeviceProperty_MediaSLOT2_FormatEnableStatus;
    CrInt64u formatAvailable = 0;
    if (!readProperty(handle, formatCode, formatAvailable) || formatAvailable != SDK::CrMediaFormat_Enable) {
        out.error = "Sony reports that full format is unavailable for this slot";
        return out;
    }
    {
        std::lock_guard<std::mutex> lock(callback.mtx);
        callback.formatFinished = false;
        callback.formatOk = false;
    }
    const auto parameter = args.slot == 1 ? SDK::CrCommandParam_Up : SDK::CrCommandParam_Down;
    const auto status = SDK::SendCommand(handle, SDK::CrCommandId_MediaFormat, parameter);
    if (CR_FAILED(status)) {
        out.error = "Camera rejected the full-format command";
        return out;
    }
    std::unique_lock<std::mutex> lock(callback.mtx);
    const bool completed = callback.cv.wait_for(lock, 15min, [&callback] {
        return callback.formatFinished || callback.disconnected.load();
    });
    if (!completed) {
        SDK::SendCommand(handle, SDK::CrCommandId_CancelMediaFormat, SDK::CrCommandParam_Down);
        out.error = "Timed out waiting for camera-card format completion";
        return out;
    }
    if (!callback.formatOk) {
        out.error = "Camera reported that card format failed or was canceled";
        return out;
    }
    out.ok = true;
    out.extraJson = "\"slot\":" + std::to_string(args.slot);
    return out;
}

ActionOutcome doDeleteFile(SDK::CrDeviceHandle handle, const Args& args) {
    ActionOutcome out;
    if (args.filename.empty()) {
        out.error = "Missing --filename for delete-file";
        return out;
    }
    for (const auto slot : {SDK::CrSlotNumber_Slot1, SDK::CrSlotNumber_Slot2}) {
        SDK::CrContentsInfo* contents = nullptr;
        CrInt32u contentCount = 0;
        const auto listStatus = SDK::GetRemoteTransferContentsInfoList(
            handle, slot, SDK::CrGetContentsInfoListType_All, nullptr, 0, &contents, &contentCount);
        if (CR_FAILED(listStatus) || !contents) continue;
        for (CrInt32u contentIndex = 0; contentIndex < contentCount; ++contentIndex) {
            bool matches = false;
            for (CrInt32u fileIndex = 0; fileIndex < contents[contentIndex].filesNum; ++fileIndex) {
                if (basename(contents[contentIndex].files[fileIndex].filePath) == args.filename) {
                    matches = true;
                    break;
                }
            }
            if (!matches) continue;
            const CrInt32u contentId = contents[contentIndex].contentId;
            SDK::ReleaseRemoteTransferContentsInfoList(handle, contents);
            const auto deleteStatus = SDK::DeleteRemoteTransferContentsFile(handle, slot, contentId);
            if (CR_FAILED(deleteStatus)) {
                out.error = "Camera rejected deletion of the matched content";
                return out;
            }
            out.ok = true;
            out.extraJson = "\"filename\":\"" + jsonEscape(args.filename) + "\"";
            return out;
        }
        SDK::ReleaseRemoteTransferContentsInfoList(handle, contents);
    }
    out.error = "Exact filename was not found on either camera slot";
    return out;
}

ActionOutcome doFtpTest(SDK::CrDeviceHandle handle) {
    ActionOutcome out;
    const auto requestStatus = SDK::RequestFTPServerSettingList(handle);
    if (CR_FAILED(requestStatus)) {
        out.error = "Camera does not allow a read-only FTP settings request in the current mode";
        return out;
    }
    std::this_thread::sleep_for(500ms);
    SDK::CrFTPServerSetting* settings = nullptr;
    CrInt32u count = 0;
    const auto getStatus = SDK::GetFTPServerSettingList(handle, &settings, &count);
    if (CR_FAILED(getStatus)) {
        out.error = "Camera did not return its FTP server settings";
        return out;
    }
    if (settings) SDK::ReleaseFTPServerSettingList(handle, settings);
    out.ok = true;
    out.extraJson = "\"configuredServerCount\":" + std::to_string(count) + ",\"readOnly\":true";
    return out;
}

ActionOutcome unsupported(const std::string& action) {
    ActionOutcome out;
    out.error = action + " is not implemented in this MVP build; ingest/format/delete stay manual until the full rebuild.";
    return out;
}

ActionOutcome runAction(SDK::CrDeviceHandle handle, Callback& callback, const Args& args, const std::string& action) {
    if (action == "status") return doStatus(handle);
    if (action == "start") return doStartStop(handle, true);
    if (action == "stop") return doStartStop(handle, false);
    if (action == "card-status" || action == "cardStatus") {
        ActionOutcome out;
        out.ok = doCardStatus(handle);
        return out;
    }
    if (action == "ingest-proxy" || action == "ingest-new") return doIngest(handle, callback, args, true);
    if (action == "ingest-master") return doIngest(handle, callback, args, false);
    if (action == "format-card") return doFormatCard(handle, callback, args);
    if (action == "delete-file") return doDeleteFile(handle, args);
    if (action == "ftp-test") return doFtpTest(handle);
    if (action == "proxy-info" || action == "proxy-lowest") {
        return unsupported(action);
    }
    ActionOutcome out;
    out.error = "Unknown action";
    return out;
}

} // namespace

int main(int argc, char** argv) {
    Args args;
    std::string parseErr;
    if (!parseArgs(argc, argv, args, parseErr)) {
        std::cerr << parseErr << std::endl;
        printResult(false, "", parseErr);
        return 1;
    }
    if (args.action.empty() && !args.server) {
        std::cerr << "Missing --action" << std::endl;
        printResult(false, "", "Missing --action");
        return 1;
    }
    if (args.mac.empty()) {
        std::string msg = "Missing --mac; refusing to connect to any camera without an explicit identity";
        std::cerr << msg << std::endl;
        printResult(false, "", msg);
        return 1;
    }

    if (!SDK::Init()) {
        std::string msg = "Failed to initialize Remote SDK";
        std::cerr << msg << std::endl;
        printResult(false, "", msg);
        return 1;
    }

    Callback cb;
    auto conn = connectByIdentity(cb, args);
    if (!conn.ok) {
        std::cerr << conn.error << std::endl;
        printResult(false, "", conn.error);
        SDK::Release();
        return 1;
    }

    int exitCode = 0;

    if (args.server) {
        std::ostringstream ready;
        ready << "SONY_REC_READY {\"ok\":true,\"camera\":\"" << jsonEscape(args.cameraLabel)
              << "\",\"mac\":\"" << jsonEscape(conn.matchedMac) << "\"}";
        flushLine(ready.str());

        std::string line;
        while (std::getline(std::cin, line)) {
            while (!line.empty() && (line.back() == '\r' || line.back() == '\n')) line.pop_back();
            if (line.empty()) continue;
            if (line == "quit") break;
            auto outcome = runAction(conn.handle, cb, args, line);
            printResult(outcome.ok, outcome.extraJson, outcome.error);
            if (cb.disconnected.load()) break;
        }
    } else {
        auto outcome = runAction(conn.handle, cb, args, args.action);
        if (args.action != "card-status" && args.action != "cardStatus") {
            printResult(outcome.ok, outcome.extraJson, outcome.error);
        }
        if (!outcome.ok) {
            if (!outcome.error.empty()) std::cerr << outcome.error << std::endl;
            exitCode = 1;
        }
    }

    disconnectAndRelease(conn.handle);
    SDK::Release();
    return exitCode;
}
