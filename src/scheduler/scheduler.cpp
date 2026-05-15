#include"scheduler.h"
#include<iostream>
#include<algorithm>
#include<fstream>
#include<thread>
#include<chrono>
#include <cerrno>
#include <filesystem>
#include <DockerClient.h>
#include <random>
#include <memory>
#include <unordered_map>
#include <arpa/inet.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>

namespace {

constexpr auto kServiceReadyTimeout = std::chrono::seconds(20);
constexpr int kDefaultSubAgentStartupTimeoutSec = 20;
constexpr auto kServiceReadyPollInterval = std::chrono::milliseconds(200);
constexpr auto kServiceReadyConnectTimeout = std::chrono::milliseconds(200);
constexpr auto kExistingSubAgentProbeTimeout = std::chrono::seconds(2);
constexpr auto kTaskServiceStartupTimeout = std::chrono::seconds(180);
constexpr int kSubAgentIdleTimeoutSec = 300;
constexpr int kMaxDeviceInfoFailureCount = 3;
std::mutex g_sub_agent_idle_timer_mutex;
std::unordered_map<std::string, std::shared_ptr<TimerCallback>> g_sub_agent_idle_timers;

struct SubAgentRuntimeConfig {
    CreateContainerParam create_param;
    int startup_timeout_sec;
};

bool IsPortListening(const std::string &ip, int port, std::chrono::milliseconds timeout) {
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        return false;
    }

    int flags = fcntl(sock, F_GETFL, 0);
    if (flags < 0 || fcntl(sock, F_SETFL, flags | O_NONBLOCK) < 0) {
        close(sock);
        return false;
    }

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (inet_pton(AF_INET, ip.c_str(), &addr.sin_addr) != 1) {
        close(sock);
        return false;
    }

    int ret = connect(sock, reinterpret_cast<sockaddr *>(&addr), sizeof(addr));
    if (ret == 0) {
        close(sock);
        return true;
    }

    if (errno != EINPROGRESS) {
        close(sock);
        return false;
    }

    fd_set writefds;
    FD_ZERO(&writefds);
    FD_SET(sock, &writefds);

    timeval tv{};
    tv.tv_sec = static_cast<long>(timeout.count() / 1000);
    tv.tv_usec = static_cast<long>((timeout.count() % 1000) * 1000);

    ret = select(sock + 1, nullptr, &writefds, nullptr, &tv);
    if (ret <= 0) {
        close(sock);
        return false;
    }

    int so_error = 0;
    socklen_t len = sizeof(so_error);
    if (getsockopt(sock, SOL_SOCKET, SO_ERROR, &so_error, &len) < 0) {
        close(sock);
        return false;
    }

    close(sock);
    return so_error == 0;
}

bool WaitForServicePort(const std::string &ip, int port, std::chrono::seconds timeout = kServiceReadyTimeout) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (std::chrono::steady_clock::now() < deadline) {
        if (IsPortListening(ip, port, kServiceReadyConnectTimeout)) {
            return true;
        }
        std::this_thread::sleep_for(kServiceReadyPollInterval);
    }
    return false;
}

void UpsertEnv(std::vector<std::string> &env_list, const std::string &key, const std::string &value) {
    const std::string prefix = key + "=";
    for (auto &item: env_list) {
        if (item.rfind(prefix, 0) == 0) {
            item = prefix + value;
            return;
        }
    }
    env_list.push_back(prefix + value);
}

std::optional<std::string> ResolveLocalIpForTarget(const std::string &target_ip, int target_port = 2375) {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        return std::nullopt;
    }

    sockaddr_in remote_addr{};
    remote_addr.sin_family = AF_INET;
    remote_addr.sin_port = htons(static_cast<uint16_t>(target_port));
    if (inet_pton(AF_INET, target_ip.c_str(), &remote_addr.sin_addr) != 1) {
        close(sock);
        return std::nullopt;
    }

    if (connect(sock, reinterpret_cast<sockaddr *>(&remote_addr), sizeof(remote_addr)) != 0) {
        close(sock);
        return std::nullopt;
    }

    sockaddr_in local_addr{};
    socklen_t len = sizeof(local_addr);
    if (getsockname(sock, reinterpret_cast<sockaddr *>(&local_addr), &len) != 0) {
        close(sock);
        return std::nullopt;
    }

    char ip_buf[INET_ADDRSTRLEN] = {0};
    const char *ip_str = inet_ntop(AF_INET, &local_addr.sin_addr, ip_buf, sizeof(ip_buf));
    close(sock);
    if (!ip_str) {
        return std::nullopt;
    }

    return std::string(ip_str);
}

std::string BuildSubAgentTimerKey(const std::string &agent_name, const DeviceID &device_id) {
    return boost::uuids::to_string(device_id) + "|" + agent_name;
}

void ArmOrRefreshSubAgentIdleTimer(const std::string &agent_name, const Device &dev, const std::string &container_name) {
    const std::string key = BuildSubAgentTimerKey(agent_name, dev.global_id);
    {
        std::lock_guard<std::mutex> lock(g_sub_agent_idle_timer_mutex);
        auto it = g_sub_agent_idle_timers.find(key);
        if (it != g_sub_agent_idle_timers.end()) {
            it->second->refresh();
            spdlog::info("Refreshed sub agent idle timer, key:{}, timeout_sec:{}", key, kSubAgentIdleTimeoutSec);
            return;
        }
    }

    auto timer = std::make_shared<TimerCallback>();
    timer->set_interval(kSubAgentIdleTimeoutSec);
    timer->set_once_flag(true);
    timer->set_callback([agent_name, dev, container_name, key]() {
        string docker_version = GetDockerVersion(dev);
        DockerClient docker_client(dev.ip_address, 2375, docker_version);
        bool delete_volume = false;
        bool force = true;
        bool delete_link_container = false;
        bool removed = docker_client.RemoveContainer(container_name, delete_volume, force, delete_link_container);
        if (removed) {
            spdlog::info("Removed idle sub agent container, key:{}, container_name:{}, ip:{}",
                         key, container_name, dev.ip_address);
        } else {
            spdlog::warn("Failed to remove idle sub agent container, key:{}, container_name:{}, ip:{}",
                         key, container_name, dev.ip_address);
        }
        std::lock_guard<std::mutex> lock(g_sub_agent_idle_timer_mutex);
        g_sub_agent_idle_timers.erase(key);
    });

    {
        std::lock_guard<std::mutex> lock(g_sub_agent_idle_timer_mutex);
        g_sub_agent_idle_timers[key] = timer;
    }
    timer->start();
    spdlog::info("Started sub agent idle timer, key:{}, timeout_sec:{}", key, kSubAgentIdleTimeoutSec);
}

void RemoveSubAgentIdleTimersForDevice(const DeviceID &device_id) {
    const std::string prefix = boost::uuids::to_string(device_id) + "|";
    std::vector<std::string> keys;
    {
        std::lock_guard<std::mutex> lock(g_sub_agent_idle_timer_mutex);
        for (const auto &[key, timer]: g_sub_agent_idle_timers) {
            if (key.rfind(prefix, 0) == 0) {
                keys.push_back(key);
            }
        }
        for (const auto &key: keys) {
            auto it = g_sub_agent_idle_timers.find(key);
            if (it != g_sub_agent_idle_timers.end()) {
                it->second->stop();
                g_sub_agent_idle_timers.erase(it);
            }
        }
    }
}

std::optional<SubAgentRuntimeConfig> LoadSubAgentRuntimeConfig(const std::string &agent_name, DeviceType device_type) {
    const auto profile_path = std::filesystem::path(ABSOLUTE_CONFIG_PATH) / "multi_agent_info.json";
    std::ifstream file(profile_path);
    if (!file.is_open()) {
        spdlog::error("failed to open sub agent profile file: {}", profile_path.string());
        return std::nullopt;
    }

    json profile_json;
    try {
        file >> profile_json;
    } catch (const std::exception &e) {
        spdlog::error("failed to parse sub agent profile file {}: {}", profile_path.string(), e.what());
        return std::nullopt;
    }

    const auto device_type_str = json(device_type).get<std::string>();
    if (!profile_json.contains("sub_agents") ||
        !profile_json["sub_agents"].contains(agent_name) ||
        !profile_json["sub_agents"][agent_name].contains("runtime") ||
        !profile_json["sub_agents"][agent_name]["runtime"].contains(device_type_str)) {
        spdlog::error("runtime config missing for agent:{} device_type:{}", agent_name, device_type_str);
        return std::nullopt;
    }

    try {
        const auto &runtime = profile_json["sub_agents"][agent_name]["runtime"][device_type_str];
        CreateContainerParam create_param(
            runtime.at("container_name").get<std::string>(),
            runtime.at("image").get<std::string>(),
            runtime.at("cmds").get<std::vector<std::string>>(),
            runtime.at("args").get<std::vector<std::string>>(),
            runtime.at("host_config_privileged").get<bool>(),
            runtime.at("env").get<std::vector<std::string>>(),
            runtime.at("host_config_binds").get<std::vector<std::string>>(),
            runtime.at("devices").get<std::vector<std::string>>(),
            runtime.at("host_ip").get<std::string>(),
            runtime.at("host_port").get<int>(),
            runtime.at("container_port").get<int>(),
            runtime.at("has_tty").get<bool>(),
            runtime.at("network_config").get<std::string>()
        );
        const int startup_timeout_sec = runtime.value("startup_timeout_sec", kDefaultSubAgentStartupTimeoutSec);
        return SubAgentRuntimeConfig{create_param, startup_timeout_sec};
    } catch (const std::exception &e) {
        spdlog::error("invalid runtime config for agent:{} device_type:{} error:{}", agent_name, device_type_str, e.what());
        return std::nullopt;
    }
}

} // namespace

std::map<TaskType, std::map<DeviceType, StaticInfoItem> > Docker_scheduler::static_info; // static task info

std::shared_mutex Docker_scheduler::devs_mutex; //
std::map<DeviceID, Device> Docker_scheduler::device_static_info; // static device info

std::map<DeviceID, DeviceStatus> Docker_scheduler::device_status; // dynamic device info
std::map<DeviceID, int> Docker_scheduler::device_info_failure_count;

// std::shared_mutex Docker_scheduler::td_map_mutex_; // Thread-safe mutex for TDMap
std::map<TaskType, std::map<DeviceID, DevSrvInfos> > Docker_scheduler::tdMap;
z3::context Docker_scheduler::c;
std::mutex Docker_scheduler::z3_mutex;

bool Docker_scheduler::is_model_loaded = false;

double get_double_value(const expr &e) {
    if (e.is_numeral()) {
        if (e.is_int()) {
            return e.get_numeral_int();
        } else {
            int num = e.numerator().get_numeral_int();
            int denom = e.denominator().get_numeral_int();
            return static_cast<double>(num) / denom;
        }
    }
    return 0.0;
}

std::string double_to_string(double value) {
    std::ostringstream oss;
    oss << std::fixed << std::setprecision(6) << value;
    return oss.str();
}

Docker_scheduler::Docker_scheduler() {
}

Docker_scheduler::Docker_scheduler(string knowledge_file) {

    ifstream infile;
    infile.open(knowledge_file, ios::in);
    //reading profiling result from infile, write them into tasks_info
    if (!infile.is_open()) {
        cerr << "Failed to open file: " << knowledge_file << endl;
        return;
    }
    // Parse the JSON file into a json object
    json json_data;
    infile >> json_data;
    infile.close();

    // Loop through the tasks in the JSON
    for (const auto &task_entry: json_data.items()) {
        // Get TaskType from key
        string task_str = task_entry.key();
        TaskType task_type;
        if (task_str == "YoloV5") task_type = YoloV5;
        else if (task_str == "MobileNet") task_type = MobileNet;
        else if (task_str == "Bert") task_type = Bert;
        else if (task_str == "ResNet50") task_type = ResNet50;
        else if (task_str == "deeplabv3") task_type = deeplabv3;
        else if (task_str == "transcoding") task_type = transcoding;
        else if (task_str == "decoding") task_type = decoding;
        else if (task_str == "encoding") task_type = encoding;
        else task_type = Unknown;
        cout << "task load:" << task_str << endl;
        // Loop through the devices for each task
        for (const auto &device_entry: task_entry.value().items()) {
            string device_str = device_entry.key();
            DeviceType device_type;
            if (device_str == "RK3588") device_type = RK3588;
            else if (device_str == "ATLAS_L") device_type = ATLAS_L;
            else if (device_str == "ATLAS_H") device_type = ATLAS_H;
            else if (device_str == "ORIN") device_type = ORIN;
            else continue;
            cout << "device load:" << device_str << endl;
            // Check if the required fields are present in the JSON before creating StaticInfoItem
            const auto &device = device_entry.value();
            if (device.contains("imageInfo") && device.contains("taskOverhead")) {
                // Create StaticInfoItem for the device only if the required fields exist
                StaticInfoItem static_info_item(device);

                // Add the StaticInfoItem to the static_info map
                static_info[task_type][device_type] = static_info_item;
            } else {
                cout << "Missing necessary fields for device: " << device_str << endl;
            }
        }
    }
    return;
}

vector<TaskType> Docker_scheduler::getTaskTypesByDeviceType(DeviceType devType) {
    vector<TaskType> res;
    for (auto [ttype,v]: static_info) {
        if (v.find(devType) != v.end()) {
            res.push_back(ttype);
        }
    }
    return res;
}


int Docker_scheduler::RegisNode(const Device &device) {
    // update devs
    device_static_info[device.global_id] = device;
    // update dev_status
    device_status[device.global_id] = DeviceStatus();
    device_info_failure_count[device.global_id] = 0;

    // update Tdmap all tasktype add new device
    // according to task_static_info, match supported tasktype and device
    vector<TaskType> supportTType = Docker_scheduler::getTaskTypesByDeviceType(device.type);
    for (auto k: supportTType) {
        // tdMap[k].emplace(std::piecewise_construct,
        //      std::forward_as_tuple(device.global_id),
        //       std::forward_as_tuple());

        // DevSrvInfos temp;
        tdMap[k].try_emplace(device.global_id); // value constructor se default
    }
    return 0;
}


void Docker_scheduler::display_dev() {
    for (auto [id,status]: device_status) {
        if(device_static_info.count(id) > 0) {
            //cout << "Device ID: " << id << endl;
            //cout << "Device IP: " << device_static_info[id].ip_address << endl;
            //cout << "Device Port: " << device_static_info[id].agent_port << endl;
            cout << "Device Type: " << device_static_info[id].type << endl;
            cout << "Device Status: " << endl;
            status.show();
        }
    }
}


void Docker_scheduler::init(string filepath) {
    loadStaticInfo(filepath);
}

ImageInfo Docker_scheduler::getImage(TaskType taskType, DeviceType devType) {
    // First, check if taskType exists
    if (static_info.count(taskType) > 0) {
        // taskType exists, now check if devType exists within taskType
        if (static_info[taskType].count(devType) > 0) {
            // Both taskType and devType exist in static_info
            // Now you can safely check imageInfo
            return static_info[taskType][devType].imageInfo;
        } else {
            throw("[taskType,devType] doesn't exist in static_info");
        }
    } else {
        // taskType doesn't exist in static_info
        throw("taskType doesn't exist in static_info");
    }
}

void Docker_scheduler::loadStaticInfo(string filepath) {
    std::ifstream file(filepath);
    if (!file.is_open()) {
        throw std::runtime_error("Could not open file");
    }
    json j;
    file >> j;
    for (auto &task: j.items()) {
        TaskType taskType;
        from_json(task.key(), taskType);
        for (auto &device: task.value().items()) {
            DeviceType deviceType;
            from_json(device.key(), deviceType);
            static_info[taskType][deviceType] = StaticInfoItem(device.value());;
        }
    }
}

void Docker_scheduler::RemoveDevice(DeviceID global_id) {
    for (auto &[ttype, dev_srv_infos_by_device]: tdMap) {
        auto it = dev_srv_infos_by_device.find(global_id);
        if (it != dev_srv_infos_by_device.end()) {
            it->second.timer_callback.stop();
            dev_srv_infos_by_device.erase(it);
        }
    }
    device_static_info.erase(global_id);
    device_status.erase(global_id);
    device_info_failure_count.erase(global_id);
    RemoveSubAgentIdleTimersForDevice(global_id);
}

bool Docker_scheduler::HotStartAllNodeByTType(TaskType ttype) {
    int support_ttype_dev_nums = 0;
    int start_container_nums = 0;
    for(auto [deviceId, devSrvInfos] : tdMap[ttype]) {
        support_ttype_dev_nums++;
        Device dev = device_static_info[deviceId];
        std::optional<SrvInfo> srvInfo = createContainerByTType(ttype, dev);
        if(srvInfo == nullopt) {
            spdlog::error("HotStartAllNodeByTType createContainer failed, ip:{}", dev.ip_address);
        }else {
            start_container_nums++;
            spdlog::info("HotStartAllNodeByTType createContainer success, ip:{}", dev.ip_address);
        }
    }
    spdlog::info("-----------HotStartAllNodeByTType createContainer info------------\n  support_ttype_dev_nums:{}, start_container_nums：{}\n", support_ttype_dev_nums, start_container_nums);
    return true;
}

void Docker_scheduler::startDeviceInfoCollection() {
    std::thread([]() {
        while (true) {
            {
                std::unique_lock<std::shared_mutex> lock(devs_mutex);
                std::vector<DeviceID> disconnected_devices;
                for (auto [k, dev]: device_static_info) {
                    bool collect_success = false;
                    // start new Thread to collect
                    httplib::Client cli(dev.ip_address, dev.agent_port);
                    httplib::Result res;
                    try {
                        res = cli.Get("/usage/device_info");

                        // update device staus
                        if (res != nullptr && res.error() == httplib::Error::Success) {
                            string restr = res->body.data();
                            json j = json::parse(restr);
                            string resp_status = j["status"];
                            if (resp_status != "success") {
                                spdlog::error(
                                        "Failed to get device info, agent return filed,dev.ip_address:{}, dev.agent_port:{}",
                                        dev.ip_address, dev.agent_port);
                            } else {
                                DeviceStatus status;
                                status.from_json(j["result"]);
                                device_status[k] = status;
                                device_info_failure_count[k] = 0;
                                collect_success = true;
                            }
//                            spdlog::info("success to get device info, dev.ip_address:{}, dev.agent_port:{}, "
//                                         "status.mem_used:{}"
//                                         "status.cpu_used:{}"
//                                         "status.xpu_used:{}{}{}", dev.ip_address, dev.agent_port,
//                                         status.mem_used,
//                                         status.cpu_used,
//                                         status.xpu_used,
//                                         status.net_latency,
//                                         status.net_bandwidth
//                            );
                        } else {
                            spdlog::error("Failed to get device info, dev.ip_address:{}, dev.agent_port:{}",
                                          dev.ip_address, dev.agent_port);
                        }
                    } catch (const std::exception &e) {
                        spdlog::error("Failed to get device info, dev.ip_address:{}, dev.agent_port:{}, error:{}",
                                      dev.ip_address, dev.agent_port, e.what());
                    }

                    if (!collect_success) {
                        int &failure_count = device_info_failure_count[k];
                        failure_count++;
                        if (failure_count >= kMaxDeviceInfoFailureCount) {
                            spdlog::error(
                                    "Device disconnected after {} consecutive device_info failures, remove node, global_id:{}, ip:{}, agent_port:{}",
                                    failure_count,
                                    boost::uuids::to_string(k),
                                    dev.ip_address,
                                    dev.agent_port);
                            disconnected_devices.push_back(k);
                        }
                    }
                }

                for (const auto &device_id: disconnected_devices) {
                    RemoveDevice(device_id);
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
        }
    }).detach();
}




// CallBack Function to delete inactive contianer
void Docker_scheduler::inactiveTimeCallback(TaskType ttype, Device dev, string container_id) {
    sleep(2); // sleep to wait questing toi finish to avoid the current quest failed
    string docker_version = GetDockerVersion(dev);
    DockerClient docker_client(dev.ip_address, 2375, docker_version);
    bool delete_volume = false;
    bool force = true;
    bool delete_link_container = false;
    // fisrt tag deleting
    tdMap[ttype][dev.global_id].dev_srv_info_status = Deleting;
    // second remove
    bool rst = docker_client.RemoveContainer(container_id, delete_volume, force, delete_link_container);
    if(rst) {
        spdlog::info("Remove Container Success,contianerid:{},TaskType:{} ,ip:{}", container_id,  to_string(nlohmann::json(ttype)), dev.ip_address);
    }else {
        spdlog::error("Remove Container failed,contianerid:{},TaskType:{},ip:{}", container_id,to_string(nlohmann::json(ttype)), dev.ip_address);
    }

    // final set noexist
    tdMap[ttype][dev.global_id].dev_srv_info_status = NoExist;
    // modify info

    std::cout << "inactiveTimeCallback triggered the callback!" << std::endl;
}

std::optional<SrvInfo> Docker_scheduler::getOrCrtSrvByTType(TaskType ttype) {
    if (tdMap.find(ttype) == tdMap.end()) {
        spdlog::error("No available service nodes to support this task type:{}", to_string(nlohmann::json(ttype)));
        return std::nullopt;
    }
    // step 1: use z3s get deviceID
    TimeRecord<chrono::milliseconds> z3("z3");
    z3.startRecord();
    Device tgt_dev = getTgtDevByTtypeAndDevIds(ttype);
    z3.endRecord();
    spdlog::info("z3 cost_time:{}", z3.getDuration());
    z3.clearRecord();

    // TODO deal no Device from schedule
    // judege there are creating Sr v
    // create a new container
    switch (tdMap[ttype][tgt_dev.global_id].dev_srv_info_status) {
        case DevSrvInfoStatus::Creating:{
            int index = 10;
            while (tdMap[ttype][tgt_dev.global_id].dev_srv_info_status == Creating || index > 0) {
                index--;
                // wait until create complete or quest time_out
                std::this_thread::sleep_for(std::chrono::seconds(1));
            }
            if (index <= 0 || tdMap[ttype][tgt_dev.global_id].dev_srv_info_status == NoExist) {
                spdlog::error(
                    "for Tasktype:{},devIp:{}, the dev is creating contianer but over 10 times try or creating failed",
                    to_string(nlohmann::json(ttype)), tgt_dev.ip_address);
                return nullopt;
            }
            // refresh timeCallBack when access
            tdMap[ttype][tgt_dev.global_id].timer_callback.refresh();
            spdlog::info("ttype:{}, ip:{} timeCallBack refresh, lastTime={}, intervalTime={}",  to_string(nlohmann::json(ttype)), tgt_dev.ip_address, tdMap[ttype][tgt_dev.global_id].timer_callback.getElapsedTime(),  tdMap[ttype][tgt_dev.global_id].timer_callback.getIntervalTime());
            return tdMap[ttype][tgt_dev.global_id].srv_infos[0];
        }
        case DevSrvInfoStatus::Running:
            // refresh timeCallBack when access
            if (tdMap[ttype][tgt_dev.global_id].srv_infos.empty() ||
                !WaitForServicePort(tgt_dev.ip_address,
                                    tdMap[ttype][tgt_dev.global_id].srv_infos[0].port,
                                    kExistingSubAgentProbeTimeout)) {
                spdlog::error("running service is not reachable, recreate container, task_type:{}, ip:{}",
                              to_string(nlohmann::json(ttype)), tgt_dev.ip_address);
                tdMap[ttype][tgt_dev.global_id].srv_infos.clear();
                tdMap[ttype][tgt_dev.global_id].dev_srv_info_status = NoExist;
                return createContainerByTType(ttype, tgt_dev);
            }
            tdMap[ttype][tgt_dev.global_id].timer_callback.refresh();
        spdlog::info("ttype:{}, ip:{} timeCallBack refresh, lastTime={}, intervalTime={}",  to_string(nlohmann::json(ttype)), tgt_dev.ip_address, tdMap[ttype][tgt_dev.global_id].timer_callback.getElapsedTime(),  tdMap[ttype][tgt_dev.global_id].timer_callback.getIntervalTime());
            return tdMap[ttype][tgt_dev.global_id].srv_infos[0];
        case DevSrvInfoStatus::NoExist:{
            std::optional<SrvInfo> srv_info = createContainerByTType(ttype, tgt_dev);
            return srv_info;
        }
        default:
            spdlog::error("unkonwn error,for Tasktype:{},,device_ip:{}, getorCrtSrvByType", to_string(nlohmann::json(ttype)), tgt_dev.ip_address);
            return nullopt;
    }
}

std::optional<SrvInfo> Docker_scheduler::getOrCrtSrvByTTypeOnDevice(TaskType ttype, const DeviceID &device_id) {
    auto device_opt = getDeviceById(device_id);
    if (!device_opt.has_value()) {
        spdlog::error("No such device for task type:{}, device_id:{}",
                      to_string(nlohmann::json(ttype)), boost::uuids::to_string(device_id));
        return std::nullopt;
    }

    if (!deviceSupportsTask(device_id, ttype)) {
        spdlog::error("Device does not support task type:{}, device_id:{}",
                      to_string(nlohmann::json(ttype)), boost::uuids::to_string(device_id));
        return std::nullopt;
    }

    Device tgt_dev = device_opt.value();
    switch (tdMap[ttype][tgt_dev.global_id].dev_srv_info_status) {
        case DevSrvInfoStatus::Creating: {
            int index = 10;
            while (tdMap[ttype][tgt_dev.global_id].dev_srv_info_status == Creating && index > 0) {
                index--;
                std::this_thread::sleep_for(std::chrono::seconds(1));
            }
            if (index <= 0 || tdMap[ttype][tgt_dev.global_id].dev_srv_info_status == NoExist) {
                spdlog::error(
                    "for Tasktype:{},devIp:{}, the target device is creating container but timed out or failed",
                    to_string(nlohmann::json(ttype)), tgt_dev.ip_address);
                return nullopt;
            }
            tdMap[ttype][tgt_dev.global_id].timer_callback.refresh();
            return tdMap[ttype][tgt_dev.global_id].srv_infos[0];
        }
        case DevSrvInfoStatus::Running:
            if (tdMap[ttype][tgt_dev.global_id].srv_infos.empty() ||
                !WaitForServicePort(tgt_dev.ip_address,
                                    tdMap[ttype][tgt_dev.global_id].srv_infos[0].port,
                                    kExistingSubAgentProbeTimeout)) {
                spdlog::error("target running service is not reachable, recreate container, task_type:{}, ip:{}",
                              to_string(nlohmann::json(ttype)), tgt_dev.ip_address);
                tdMap[ttype][tgt_dev.global_id].srv_infos.clear();
                tdMap[ttype][tgt_dev.global_id].dev_srv_info_status = NoExist;
                return createContainerByTType(ttype, tgt_dev);
            }
            tdMap[ttype][tgt_dev.global_id].timer_callback.refresh();
            return tdMap[ttype][tgt_dev.global_id].srv_infos[0];
        case DevSrvInfoStatus::NoExist:
            return createContainerByTType(ttype, tgt_dev);
        default:
            spdlog::error("unknown error,for Tasktype:{},device_ip:{}, getOrCrtSrvByTTypeOnDevice",
                          to_string(nlohmann::json(ttype)), tgt_dev.ip_address);
            return nullopt;
    }
}

std::optional<SrvInfo> Docker_scheduler::createContainerByTType(TaskType ttype, const Device &dev) {
    DeviceType dtype = dev.type;
    StaticInfoItem static_info_item = static_info[ttype][dtype];
    ImageInfo image_info = static_info_item.imageInfo;
    CreateContainerParam cparam = CreateContainerParam(
        image_info.container_name,
        image_info.image,
        image_info.cmds,
        image_info.args,
        image_info.host_config_privileged,
        image_info.env,
        image_info.host_config_binds,
        image_info.devices,
        image_info.host_ip,
        image_info.host_port,
        image_info.container_port,
        image_info.has_tty,
        image_info.network_config
    );
    string docker_version = GetDockerVersion(dev);
    DockerClient docker_client(dev.ip_address, 2375, docker_version);
    // first set Creating tag
    tdMap[ttype][dev.global_id].dev_srv_info_status = Creating;
    tdMap[ttype][dev.global_id].srv_infos.clear();
    // then invoke api
    string container_id = docker_client.CreateContainer(cparam);
    bool container_started = false;
    if (container_id.empty()) {
        spdlog::error("docker_client.CreateContainer failed, try start existing container by name, para:{}", cparam.toString());
        container_id = cparam.container_name;
        if (!docker_client.StartContainer(container_id)) {
            tdMap[ttype][dev.global_id].dev_srv_info_status = NoExist;
            spdlog::error("docker start existing container failed, container_name={}", container_id);
            return nullopt;
        }
        container_started = true;
    } else {
        spdlog::info("docker_client.CreateContainer Success, para:{}, ret:{}", container_id, cparam.toString());
    }

    bool start_res = container_started || docker_client.StartContainer(container_id);
    if (!start_res) {
        tdMap[ttype][dev.global_id].dev_srv_info_status = NoExist;
        spdlog::error("docker start container failed, container_id={}", container_id);
        return nullopt;
    }

    if (!WaitForServicePort(dev.ip_address, static_info_item.imageInfo.host_port, kTaskServiceStartupTimeout)) {
        tdMap[ttype][dev.global_id].dev_srv_info_status = NoExist;
        spdlog::error("service port not ready after container start, container_id={}, ip={}, port={}, timeout_sec:{}",
                      container_id,
                      dev.ip_address,
                      static_info_item.imageInfo.host_port,
                      kTaskServiceStartupTimeout.count());
        return nullopt;
    }

    // final set running tag
    SrvInfo srv_info{"", dev.ip_address, static_info_item.imageInfo.host_port};
    tdMap[ttype][dev.global_id].srv_infos.push_back(srv_info);
    tdMap[ttype][dev.global_id].dev_srv_info_status = Running;

    // start a timeCallback to delete inactive container
    auto bind_inactiveTimeCallback = std::bind(inactiveTimeCallback, ttype, dev, container_id);
    tdMap[ttype][dev.global_id].timer_callback.set_interval(600);
    tdMap[ttype][dev.global_id].timer_callback.set_callback(bind_inactiveTimeCallback);
    tdMap[ttype][dev.global_id].timer_callback.set_once_flag(true);
    tdMap[ttype][dev.global_id].timer_callback.start();

    return srv_info;
}

Device Docker_scheduler::getTgtDevByTtype(TaskType ttype) {
    std::vector<DeviceID> deviceIDs;
    for (const auto &pair: tdMap[ttype]) {
        deviceIDs.push_back(pair.first); // pair.first 是 DeviceID
    }

    return getTgtDevByTtypeAndDevIds(ttype, deviceIDs);
}

Device Docker_scheduler::getTgtDevByTtypeAndDevIds(TaskType ttype) {
    static int callCount = 0;
    static Device tgt_dev;
    //return Z3_simulate_schedule(ttype, 0, 1, 0); //ATLAS-H ATLAS-L RK3588

    //tgt_dev = Z3_schedule_v2(ttype);
    //tgt_dev = Model_predict(ttype);
    if (callCount %20 == 0) {
        callCount=0;
        //tgt_dev = Model_predict(ttype);
        tgt_dev = Z3_schedule_v2(ttype);
    }
    callCount++;  // 每次调用+1
    return tgt_dev;
}


Device Docker_scheduler::getTgtDevByTtypeAndDevIds(TaskType ttype, vector<DeviceID> devIds) {
    DeviceID tgt_dev_id = devIds[0];
    return Z3_schedule_v2(ttype);
}


std::map<TaskType, std::map<DeviceType, StaticInfoItem> > Docker_scheduler::getStaticInfo() {
    return static_info;
}

json Docker_scheduler::getClusterResources() {
    json nodes = json::array();

    std::shared_lock<std::shared_mutex> lock(devs_mutex);
    for (const auto &[device_id, device]: device_static_info) {
        json node;
        node["global_id"] = boost::uuids::to_string(device_id);
        node["type"] = json(device.type);
        node["ip_address"] = device.ip_address;
        node["agent_port"] = device.agent_port;

        auto status_it = device_status.find(device_id);
        if (status_it != device_status.end()) {
            node["resource"] = status_it->second.to_json();
        } else {
            node["resource"] = nullptr;
        }

        nodes.push_back(node);
    }
    spdlog::info("Finished fetching cluster resources");
    return nodes;
}

std::optional<Device> Docker_scheduler::scheduleDeviceByTType(TaskType ttype) {
    if (ttype == TaskType::Unknown || static_info.find(ttype) == static_info.end()) {
        spdlog::error("Cannot schedule unknown or unsupported task type:{}", to_string(nlohmann::json(ttype)));
        return std::nullopt;
    }
    if (tdMap.find(ttype) == tdMap.end() || tdMap[ttype].empty()) {
        spdlog::error("No registered device supports task type:{}", to_string(nlohmann::json(ttype)));
        return std::nullopt;
    }

    std::shared_lock<std::shared_mutex> lock(devs_mutex);
    std::optional<Device> best_device;
    double best_score = std::numeric_limits<double>::max();

    for (const auto &[device_id, device]: device_static_info) {
        auto task_profile_it = static_info[ttype].find(device.type);
        if (task_profile_it == static_info[ttype].end()) {
            continue;
        }

        double cpu_used = 0.0;
        double mem_used = 0.0;
        double xpu_used = 0.0;
        auto status_it = device_status.find(device_id);
        if (status_it != device_status.end()) {
            cpu_used = status_it->second.cpu_used;
            mem_used = status_it->second.mem_used;
            xpu_used = status_it->second.xpu_used;
        }

        const auto srv_it = tdMap[ttype].find(device_id);
        const bool cold_start = srv_it == tdMap[ttype].end() ||
                                srv_it->second.dev_srv_info_status == DevSrvInfoStatus::NoExist;
        if (cold_start) {
            const auto &overhead = task_profile_it->second.taskOverhead;
            cpu_used += overhead.cpu_usage;
            mem_used += overhead.mem_usage;
            xpu_used += overhead.xpu_usage;
        }

        if (cpu_used > 1.0 || mem_used > 1.0 || xpu_used > 1.0) {
            continue;
        }

        const double score = cpu_used + xpu_used + 0.1 * mem_used;
        if (!best_device.has_value() || score < best_score) {
            best_device = device;
            best_score = score;
        }
    }

    if (!best_device.has_value()) {
        spdlog::error("No device has enough remaining resources for task type:{}",
                      to_string(nlohmann::json(ttype)));
        return std::nullopt;
    }

    spdlog::info("Gateway scheduler selected device, task_type:{}, target_global_id:{}, score:{}",
                 to_string(nlohmann::json(ttype)),
                 boost::uuids::to_string(best_device->global_id),
                 best_score);
    return best_device;
}

std::optional<SrvInfo> Docker_scheduler::startSubAgentOnDevice(const std::string &agent_name, const DeviceID &device_id) {
    auto device_opt = getDeviceById(device_id);
   
    if (!device_opt.has_value()) {
        spdlog::error("No such device when starting sub agent:{}, device_id:{}", agent_name, boost::uuids::to_string(device_id));
        return std::nullopt;
    }

    Device dev = device_opt.value();
    auto runtime_config_opt = LoadSubAgentRuntimeConfig(agent_name, dev.type);
    if (!runtime_config_opt.has_value()) {
        return std::nullopt;
    }

    const auto runtime_config = runtime_config_opt.value();
    CreateContainerParam cparam = runtime_config.create_param;
    if (auto gateway_host_ip = ResolveLocalIpForTarget(dev.ip_address); gateway_host_ip.has_value()) {
        UpsertEnv(cparam.env, "GATEWAY_HOST", gateway_host_ip.value());
        spdlog::info("Resolved dynamic gateway host for sub agent, agent_name:{}, target_ip:{}, gateway_host:{}",
                     agent_name, dev.ip_address, gateway_host_ip.value());
    } else {
        spdlog::warn("Failed to resolve dynamic gateway host for sub agent, keep configured GATEWAY_HOST, agent_name:{}, target_ip:{}",
                     agent_name, dev.ip_address);
    }
    const auto startup_timeout = std::chrono::seconds(std::max(runtime_config.startup_timeout_sec, 1));
    string docker_version = GetDockerVersion(dev);
    DockerClient docker_client(dev.ip_address, 2375, docker_version);

    spdlog::info("Searching for existing sub agent, agent_name:{}, ip:{}, port:{}, probe_timeout_sec:{}",
                 agent_name, dev.ip_address, cparam.host_port, kExistingSubAgentProbeTimeout.count());
    if (WaitForServicePort(dev.ip_address, cparam.host_port, kExistingSubAgentProbeTimeout)) {
        spdlog::info("Sub agent already reachable, agent_name:{}, ip:{}, port:{}", agent_name, dev.ip_address, cparam.host_port);
        ArmOrRefreshSubAgentIdleTimer(agent_name, dev, cparam.container_name);
        return SrvInfo{"", dev.ip_address, cparam.host_port};
    }

    spdlog::info("Creating or starting container, agent_name:{}, container_name:{}, image:{}",
                 agent_name, cparam.container_name, cparam.image);
    string container_id = docker_client.CreateContainer(cparam);
    if (!container_id.empty()) {
        spdlog::info("Starting newly created sub agent container, agent_name:{}, container_id:{}", agent_name, container_id);
        if (!docker_client.StartContainer(container_id)) {
            spdlog::error("Failed to start newly created sub agent container, agent_name:{}, container_id:{}", agent_name, container_id);
            return std::nullopt;
        }
    } else {
        spdlog::info("Sub agent container already exist, try to start by name, agent_name:{}, container_name:{}", agent_name, cparam.container_name);
        docker_client.StartContainer(cparam.container_name);
    }

    spdlog::info("Waiting for sub agent initializing, agent_name:{}, ip:{}, port:{}, timeout_sec:{}",
                 agent_name, dev.ip_address, cparam.host_port, runtime_config.startup_timeout_sec);
    if (!WaitForServicePort(dev.ip_address, cparam.host_port, startup_timeout)) {
        spdlog::error("Sub agent port not ready after startup, agent_name:{}, ip:{}, port:{}, timeout_sec:{}",
                      agent_name, dev.ip_address, cparam.host_port, runtime_config.startup_timeout_sec);
        return std::nullopt;
    }

    ArmOrRefreshSubAgentIdleTimer(agent_name, dev, cparam.container_name);
    return SrvInfo{"", dev.ip_address, cparam.host_port};
}

std::optional<Device> Docker_scheduler::getDeviceById(const DeviceID &device_id) {
    std::shared_lock<std::shared_mutex> lock(devs_mutex);
    auto it = device_static_info.find(device_id);
    if (it == device_static_info.end()) {
        return std::nullopt;
    }
    return it->second;
}

bool Docker_scheduler::deviceSupportsTask(const DeviceID &device_id, TaskType ttype) {
    std::shared_lock<std::shared_mutex> lock(devs_mutex);
    auto dev_it = device_static_info.find(device_id);
    if (dev_it == device_static_info.end()) {
        return false;
    }

    auto task_it = static_info.find(ttype);
    if (task_it == static_info.end()) {
        return false;
    }

    return task_it->second.find(dev_it->second.type) != task_it->second.end();
}

//尝试修改调度单位为单个任务
Device Docker_scheduler::Z3_schedule_v2(TaskType Ttype){
    std::lock_guard<std::mutex> lock(z3_mutex);
    //std::cout << "Executing Z3_schedule..." << std::endl;

    optimize opt(c);

    // 保存设备的 Z3 变量和其对应的负载表达式
    std::map<DeviceID, std::optional<expr>> device_cpu_load_vars;
    std::map<DeviceID, std::optional<expr>> device_mem_load_vars;
    std::map<DeviceID, std::optional<expr>> device_xpu_load_vars;

    std::vector<expr> load_differences;

    // 为每个设备创建负载变量
    for (const auto& [device_id, status] : device_status) {
        device_cpu_load_vars[device_id] = c.real_val(std::to_string(status.cpu_used).c_str());
        device_mem_load_vars[device_id] = c.real_val(std::to_string(status.mem_used).c_str());
        device_xpu_load_vars[device_id] = c.real_val(std::to_string(status.xpu_used).c_str());
    }

    // 添加任务的 Profiling 数据为约束
    expr maxV = c.real_val(double_to_string(1.0).c_str());
    const auto& task_profiling = static_info[Ttype];
    
    for (const auto& [device_type, profiling_info] : task_profiling) {
        for (const auto& [device_id, status] : device_status) {
            if (device_static_info[device_id].type == device_type) {
                // 约束：任务的资源需求不能超过设备剩余容量
                expr prof_cpu = c.real_val(double_to_string(profiling_info.taskOverhead.cpu_usage).c_str());
                expr prof_mem = c.real_val(double_to_string(profiling_info.taskOverhead.mem_usage).c_str());
                expr prof_xpu = c.real_val(double_to_string(profiling_info.taskOverhead.xpu_usage).c_str());
                
                //首先判断是否有任务在运行，如果没有意味着需要冷启动，则应该将profiling数据加入约束
                if(tdMap[Ttype][device_id].dev_srv_info_status == NoExist){
                    expr cpu = *device_cpu_load_vars[device_id] + prof_cpu;
                    expr mem = *device_mem_load_vars[device_id] + prof_mem;
                    expr xpu = *device_xpu_load_vars[device_id] + prof_xpu;
                    device_cpu_load_vars[device_id] = cpu;
                    device_mem_load_vars[device_id] = mem;
                    device_xpu_load_vars[device_id] = xpu;
                    opt.add(cpu <= maxV);
                    opt.add(mem <= maxV);
                    opt.add(xpu <= maxV);
                }else{
                    //如果有任务在运行，就不需要加入profiling数据
                    expr cpu = *device_cpu_load_vars[device_id];
                    expr mem = *device_mem_load_vars[device_id];
                    expr xpu = *device_xpu_load_vars[device_id];
//                    if(device_type==DeviceType::ATLAS_L){//针对低算力板子做约束
//                        z3::expr factor = c.real_val("1.5");
//                        cpu = cpu * factor;
//                        mem = mem * factor;
//                        xpu = xpu * factor;
//                        cout<<"choose atlas_l"<<endl;
//                    }
                    opt.add(cpu <= maxV);
                    opt.add(mem <= maxV);
                    opt.add(xpu <= maxV);

                    std::cout << "Estimated load exprs for Device " << boost::uuids::to_string(device_id)
                            << " Estimated CPU: " << get_double_value(cpu)
                            << " Estimated MEM: " << get_double_value(mem)
                            << " Estimated XPU: " << get_double_value(xpu)
                            << std::endl;
                }


                // std::cout << "Initial load for Device " << boost::uuids::to_string(device_id)
                //         << " CPU: " << get_double_value(*device_cpu_load_vars[device_id])
                //         << " MEM: " << get_double_value(*device_mem_load_vars[device_id])
                //         << " XPU: " << get_double_value(*device_xpu_load_vars[device_id])
                //         << std::endl;
                // std::cout << "Task Profiling Data for Device " << boost::uuids::to_string(device_id)
                //         << " CPU: " << get_double_value(prof_cpu)
                //         << " MEM: " << get_double_value(prof_mem)
                //         << " XPU: " << get_double_value(prof_xpu)
                //         << std::endl;
                // std::cout << "Estimated load exprs for Device " << boost::uuids::to_string(device_id)
                //         << " Estimated CPU: " << get_double_value(cpu)
                //         << " Estimated MEM: " << get_double_value(mem)
                //         << " Estimated XPU: " << get_double_value(xpu)
                //         << std::endl;
                //getchar();
            }
        }
    }
    

    expr weighted_load = c.real_val(0);
    for (const auto& [device_id, load_var] : device_cpu_load_vars) {
        weighted_load = weighted_load +
                        (0.5 * *device_cpu_load_vars[device_id] +
                        0.0 * *device_mem_load_vars[device_id] +
                        0.5 * *device_xpu_load_vars[device_id]);
    }
    opt.minimize(weighted_load);

    // 求解
    if (opt.check() == sat) {
        model m = opt.get_model();
        DeviceID selected_device_id;
        double min_load = std::numeric_limits<double>::max();

        // 查找负载最小的设备
        for (const auto& [device_id, load_var] : device_cpu_load_vars) {
            double cpu_load = get_double_value(m.eval(*load_var));
            double mem_load = get_double_value(m.eval(*device_mem_load_vars[device_id]));
            double xpu_load = get_double_value(m.eval(*device_xpu_load_vars[device_id]));
            //double total_load = cpu_load + xpu_load;
//            std::cout << "Device status after schedule" << boost::uuids::to_string(device_id)
//          << " CPU: " << get_double_value(m.eval(*load_var))
//          << " MEM: " << get_double_value(m.eval(*device_mem_load_vars[device_id]))
//          << " XPU: " << get_double_value(m.eval(*device_xpu_load_vars[device_id]))
//          << std::endl;
            //getchar();
            if (cpu_load <= 1.0 && mem_load <= 1.0 && xpu_load <= 1.0) { // 确保约束生效
                double total_load = cpu_load+ xpu_load;
                if (total_load < min_load) {
                    min_load = total_load;
                    selected_device_id = device_id;
                }
            }else{
                throw std::runtime_error("Z3 could not find a suitable device.");
            }

        }

        std::cout << "Selected device: " << boost::uuids::to_string(selected_device_id) << " with load: " << min_load << std::endl;

        return device_static_info[selected_device_id];
    } else {
        DeviceID selected_device_id = device_static_info.begin()->first;
        cout<<"Z3 could not find a suitable device, select the first device by default."<<endl;
        return device_static_info[selected_device_id]; // 默认返回第一个设备
    }
}
    

Device Docker_scheduler::Z3_simulate_schedule(TaskType Ttype,float prob1, float prob2, float prob3) {
     //find device_static_info and print dev informations
    std::vector<DeviceID> device_ids;
    for (const auto &[device_id, device]: device_static_info) {
        // print dev information
        //std::cout << "Device ID: " << device_id << ", Type: " << device.type << std::endl;
        device_ids.push_back(device_id);
    }
    // check the number of devices
    // if (device_ids.size() < 3) {
    //     throw std::runtime_error("设备数量不足3个，无法根据概率选择设备");
    // }

    // check the sum of probabilities
    if (prob1 + prob2 + prob3 != 1.0f) {
        throw std::invalid_argument("输入的概率参数不合法：prob1 + prob2 + prob3 必须等于 1");
    }

    // produce a random number
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_real_distribution<> dis(0.0, 1.0);

    float random_value = dis(gen);

// choose the device based on the random value
    for (const auto &[device_id, device] : device_static_info) {
        if (device.type == DeviceType::ATLAS_H && random_value >= 0.0f && random_value < prob1) {
            cout << "DEVICE ATLAS-H" << endl;
            return device;
        } else if (device.type == DeviceType::ATLAS_L && random_value >= prob1 && random_value < prob1 + prob2) {
            cout << "DEVICE ATLAS-L" << endl;
            return device;
        } else if (device.type == DeviceType::RK3588 && random_value >= prob1 + prob2 && random_value < 1.0f) {
            cout << "DEVICE RK3588" << endl;
            return device;
        }
    }
    throw std::runtime_error("can not decide a device");

}

struct Predict_data {
    double cpu_used;
    double xpu_used;
    double cpu_square;
    double xpu_square;
    double cpu_xpu;
    int platform;
    int tasktype;
};

int Docker_scheduler::encodeTaskType(TaskType Ttype) {
    switch (Ttype) {
        case TaskType::Bert: return 0;
        case TaskType::MobileNet: return 1;
        case TaskType::ResNet50: return 2;
        case TaskType::YoloV5: return 3;
        case TaskType::deeplabv3: return 4;
        default: throw std::invalid_argument("Unknown TaskType");
    }
}

int Docker_scheduler::encodePlatform(DeviceType dtype) {
    switch (dtype) {
        case DeviceType::ATLAS_H: return 0;
        case DeviceType::ATLAS_L: return 1;
        case DeviceType::RK3588: return 2;
        default: throw std::invalid_argument("Unknown DeviceType");
    }
}
