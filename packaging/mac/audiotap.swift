// Records one app's sound and silences it, with no driver and no change to the user's audio settings: a Core Audio
// process tap (macOS 14.2 and later). The studio starts this for the hidden Spotify player.
//
//     audiotap <pid> [<pid> ...]
//
// First line on stdout: {"rate": 48000, "channels": 2}. Then raw interleaved 32-bit float frames until stdin closes,
// which is how the studio ends it. Nothing is written to disk. macOS asks the user once for permission to record audio.

import AudioToolbox
import CoreAudio
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("audiotap: \(message)\n".data(using: .utf8)!)
    exit(1)
}

func check(_ status: OSStatus, _ what: String) {
    if status != noErr { fail("\(what) failed (\(status))") }
}

func processObject(pid: pid_t) -> AudioObjectID {
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
                                             mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var pid = pid
    var object = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, UInt32(MemoryLayout<pid_t>.size), &pid, &size, &object),
          "finding process \(pid)")
    if object == AudioObjectID(kAudioObjectUnknown) { fail("process \(pid) has no audio") }
    return object
}

let pids = CommandLine.arguments.dropFirst().compactMap { pid_t($0) }
if pids.isEmpty { fail("usage: audiotap <pid> [<pid> ...]") }

let description = CATapDescription(stereoMixdownOfProcesses: pids.map { processObject(pid: $0) })
description.uuid = UUID()
description.name = "Light Show Studio"
description.isPrivate = true
description.muteBehavior = .mutedWhenTapped          // the room hears the studio's delayed copy, never this

var tap = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateProcessTap(description, &tap), "creating the tap (is audio recording allowed for this app?)")

// The recording device: the tap, clocked by the default output (a device of its own never runs without a real one in it)
func defaultOutputUID() -> String? {
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                             mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var output = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &output) == noErr else { return nil }
    address.mSelector = kAudioDevicePropertyDeviceUID
    var uid: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(output, &address, 0, nil, &size, &uid) == noErr else { return nil }
    return uid?.takeRetainedValue() as String?
}

var aggregate: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Light Show Studio",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]],
]
if let output = defaultOutputUID() {
    aggregate[kAudioAggregateDeviceMainSubDeviceKey] = output
    aggregate[kAudioAggregateDeviceSubDeviceListKey] = [[kAudioSubDeviceUIDKey: output]]
}
var device = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &device), "creating the recording device")

var format = AudioStreamBasicDescription()
var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
var formatAddress = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
check(AudioObjectGetPropertyData(tap, &formatAddress, 0, nil, &formatSize, &format), "reading the tap's format")

print("{\"rate\": \(Int(format.mSampleRate)), \"channels\": \(format.mChannelsPerFrame)}")
fflush(stdout)

var procID: AudioDeviceIOProcID?
let queue = DispatchQueue(label: "audiotap")
check(AudioDeviceCreateIOProcIDWithBlock(&procID, device, queue) { _, input, _, _, _ in
    // The tap's stream comes last: an output device with inputs of its own (a USB interface) adds streams before it
    if let buffer = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input)).last, let data = buffer.mData {
        fwrite(data, 1, Int(buffer.mDataByteSize), stdout)
    }
    fflush(stdout)
}, "starting the recording")
check(AudioDeviceStart(device, procID), "starting the recording")

// The studio closes stdin when it is done, or by ending
DispatchQueue.global().async {
    var byte: UInt8 = 0
    while read(0, &byte, 1) > 0 {}
    DispatchQueue.global().asyncAfter(deadline: .now() + 2) { exit(0) }      // (ending the process undoes a private tap anyway)
    AudioDeviceStop(device, procID)
    if let procID { AudioDeviceDestroyIOProcID(device, procID) }
    AudioHardwareDestroyAggregateDevice(device)
    AudioHardwareDestroyProcessTap(tap)
    exit(0)
}
dispatchMain()
