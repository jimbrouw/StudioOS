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
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
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
        else if (a == "--server") out.server = true;
        // Accept and ignore legacy flags so old config entries don't hard-fail.
        else if (a == "--dest" || a == "--filename" || a == "--slot") { next(a.c_str()); }
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

// Prints the SONY_REC_CARD_STATUS line the dashboard expects. Read-only.
bool doCardStatus(SDK::CrDeviceHandle handle) {
    CrInt64u recState = 0;
    bool recordingOk = readProperty(handle, SDK::CrDeviceProperty_RecordingState, recState);
    bool recording = recordingOk && (recState == SDK::CrMovie_Recording_State_Recording);

    struct SlotInfo { int slot; bool present; CrInt64u remainingMinutes; };
    SlotInfo slots[2] = {{1, false, 0}, {2, false, 0}};

    struct SlotCodes { CrInt32u status, remaining; };
    SlotCodes codes[2] = {
        {SDK::CrDeviceProperty_MediaSLOT1_Status, SDK::CrDeviceProperty_MediaSLOT1_RemainingTime},
        {SDK::CrDeviceProperty_MediaSLOT2_Status, SDK::CrDeviceProperty_MediaSLOT2_RemainingTime},
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
        // Format is never offered in this MVP build (see the format-card stub).
        oss << "{\"slot\":" << slots[s].slot
            << ",\"remainingMinutes\":" << slots[s].remainingMinutes
            << ",\"fullFormatAvailable\":false,\"quickFormatAvailable\":false}";
    }
    oss << "]}";
    flushLine(oss.str());
    return true;
}

ActionOutcome unsupported(const std::string& action) {
    ActionOutcome out;
    out.error = action + " is not implemented in this MVP build; ingest/format/delete stay manual until the full rebuild.";
    return out;
}

ActionOutcome runAction(SDK::CrDeviceHandle handle, const std::string& action) {
    if (action == "status") return doStatus(handle);
    if (action == "start") return doStartStop(handle, true);
    if (action == "stop") return doStartStop(handle, false);
    if (action == "card-status" || action == "cardStatus") {
        ActionOutcome out;
        out.ok = doCardStatus(handle);
        return out;
    }
    if (action == "ingest-proxy" || action == "ingest-new" || action == "ingest-master" ||
        action == "proxy-info" || action == "proxy-lowest" || action == "format-card" ||
        action == "delete-file" || action == "ftp-test") {
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
            auto outcome = runAction(conn.handle, line);
            printResult(outcome.ok, outcome.extraJson, outcome.error);
            if (cb.disconnected.load()) break;
        }
    } else {
        auto outcome = runAction(conn.handle, args.action);
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
