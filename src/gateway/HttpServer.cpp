#include "HttpServer.h"
#include "httplib.h"
#include <algorithm>
#include <random>
#include <sstream>
#include <iostream>
#include <cstring>
#include <unordered_map>
#include <nlohmann/json.hpp>
#include <utility>
#include "DockerClient.h"
#include "TimeRecorder.h"

using json = nlohmann::json;

namespace {

std::string Base64Encode(const std::string &input) {
    static constexpr char kBase64Chars[] =
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789+/";
    std::string output;
    int val = 0;
    int valb = -6;
    for (unsigned char c: input) {
        val = (val << 8) + c;
        valb += 8;
        while (valb >= 0) {
            output.push_back(kBase64Chars[(val >> valb) & 0x3F]);
            valb -= 6;
        }
    }
    if (valb > -6) {
        output.push_back(kBase64Chars[((val << 8) >> (valb + 8)) & 0x3F]);
    }
    while (output.size() % 4) {
        output.push_back('=');
    }
    return output;
}

bool ForwardMultipartRequest(const httplib::Request &req,
                             httplib::Response &res,
                             const std::string &task_type_str,
                             const std::string &real_url_param,
                             const SrvInfo &srv_info,
                             TimeRecord<std::chrono::milliseconds> &time_record,
                             TimeRecord<std::chrono::milliseconds> &time_record_schedule) {
    std::string origin_host_ip = req.remote_addr;
    int origin_host_port = req.remote_port;
    std::string transfer_host_ip = req.local_addr;
    int transfer_host_port = req.local_port;
    std::string tgt_host_ip = srv_info.ip;
    int tgt_host_port = srv_info.port;

    httplib::Client cli(tgt_host_ip, tgt_host_port);
    httplib::MultipartFormDataItems items;
    for (auto file: req.files) {
        items.push_back(file.second);
    }
    for (const auto &param: req.params) {
        const auto &key = param.first;
        if (key == "taskid" || key == "target_global_id" || key == "real_url") {
            continue;
        }
        items.push_back({key, param.second, "", ""});
    }

    httplib::Result response;
    try {
        response = cli.Post("/" + real_url_param, items);
    } catch (const std::exception &e) {
        std::cerr << req.path << "  Error sending request: " << e.what() << std::endl;
        res.status = 500;
        res.set_content("Internal Server Error", "text/plain");
        return false;
    }

    if (response != nullptr && response->status != -1) {
        res.status = response->status;
        const auto response_content_type = response->get_header_value("Content-Type");

        time_record.endRecord();
        spdlog::info(
            "URL: {},task_type_str:{}, real_url_param:{} origin_host_ip: {}:{}, transfer_host_ip: {}:{}, tgt_host_ip: {}:{}, duration_time:{}, time_record_schedule:{}",
            req.path,
            task_type_str,
            real_url_param,
            origin_host_ip,
            origin_host_port,
            transfer_host_ip,
            transfer_host_port,
            tgt_host_ip,
            tgt_host_port,
            time_record.getDuration(),
            time_record_schedule.getDuration()
        );
        try {
            nlohmann::json jsonData = nlohmann::json::parse(response->body);
            jsonData["gateway_time"] = (double) (time_record.getDuration());
            res.set_content(jsonData.dump(), "application/json");
        } catch (const std::exception &) {
            nlohmann::json jsonData;
            jsonData["status"] = "success";
            jsonData["content_type"] = response_content_type.empty() ? "application/octet-stream" : response_content_type;
            jsonData["binary_b64"] = Base64Encode(response->body);
            jsonData["gateway_time"] = (double) (time_record.getDuration());
            res.set_content(jsonData.dump(), "application/json");
        }
        return true;
    }

    res.status = 502;
    time_record.endRecord();
    res.set_content("Bad Gateway", "text/plain");
    spdlog::error(
        "URL: {},task_type_str:{}, real_url_param:{} origin_host_ip: {}:{}, transfer_host_ip: {}:{}, tgt_host_ip: {}:{}, duration_time:{}, Error:{}",
        req.path,
        task_type_str,
        real_url_param,
        origin_host_ip,
        origin_host_port,
        transfer_host_ip,
        transfer_host_port,
        tgt_host_ip,
        tgt_host_port,
        time_record.getDuration(),
        "Connection failed or response = nullptr"
    );
    return false;
}

}

HttpServer::HttpServer(std::string ip, const int port, string absoulte_config_path) : ip(std::move(ip)), port(port) {
}

bool HttpServer::Start() {
    httplib::Server svr;
    // 注册路由
    svr.Post(QUSET_ROUTE, this->HandleQuest);
    svr.Post(QUEST_ON_NODE_ROUTE, this->HandleQuestOnNode);
    svr.Post(REGISTER_NODE_ROUTE, this->HandleRegisterNode);
    svr.Post("/hot_start", this->HandleHotStart);
    svr.Get(CLUSTER_RESOURCES_ROUTE, this->HandleClusterResources);
    svr.Get(SCHEDULE_TASK_ROUTE, this->HandleScheduleTask);
    svr.Post(SCHEDULE_TASK_ROUTE, this->HandleScheduleTask);
    spdlog::info("HttpServer started success，ip:{} port:{}",this->ip, this->port);
    auto result = svr.listen(this->ip, this->port);
    if (!result) {
        std::cerr << "HttpServer start failed！" << std::endl;
        return false;
    }
    return true;
}

// Define a function to modify or insert a key-value pair
void modifyOrInsert(httplib::Headers &headers,
                    const std::string &key, const std::string &newValue) {
    // Check if the key exists
    auto range = headers.equal_range(key);
    if (range.first != range.second) {
        // If found, modify the value for all matching keys
        for (auto it = range.first; it != range.second; ++it) {
            it->second = newValue;
        }
    } else {
        // If not found, insert a new key-value pair
        headers.emplace(key, newValue);
    }
}

//
// transfer request1
// example url: requet?taskid=0&real_url=hello/lxs
void HttpServer::HandleQuest(const httplib::Request &req, httplib::Response &res) {
    TimeRecord<std::chrono::milliseconds> time_record("HandleQuest");
    time_record.startRecord();

    TimeRecord<std::chrono::milliseconds> time_record_schedule("schedule");
    time_record_schedule.startRecord();

    // parse params
    auto task_type_str = req.get_param_value("taskid");
    auto real_url_param = req.get_param_value("real_url");
    TaskType task_type;
    // validate params
    try {
        task_type = StrToTaskType(task_type_str);
    } catch (const std::invalid_argument &e) {
        res.status = 400; // Bad Request
        res.set_content("invalid format taskid", "text/plain");
        return;
    }
    if (task_type == TaskType::Unknown) {
        res.status = 400; // Bad Request
        res.set_content("Missing taskid or real_url parameter", "text/plain");
        return;
    }
    if (real_url_param.empty()) {
        res.status = 400; // Bad Request
        res.set_content("Missing taskid or real_url parameter", "text/plain");
        return;
    }
    // get target_device_id
    optional<SrvInfo> srv_info_opt = Docker_scheduler::getOrCrtSrvByTType(task_type);
    if (srv_info_opt == nullopt) {
        res.status = 400; // Bad Request
        res.set_content("we can't get a useful srv", "text/plain");
        return;
    }
    // print in the end of quest
    time_record_schedule.endRecord();
    ForwardMultipartRequest(req, res, task_type_str, real_url_param, srv_info_opt.value(), time_record, time_record_schedule);
}

void HttpServer::HandleQuestOnNode(const httplib::Request &req, httplib::Response &res) {
    TimeRecord<std::chrono::milliseconds> time_record("HandleQuestOnNode");
    time_record.startRecord();

    TimeRecord<std::chrono::milliseconds> time_record_schedule("schedule_on_node");
    time_record_schedule.startRecord();

    auto task_type_str = req.get_param_value("taskid");
    auto target_global_id_str = req.get_param_value("target_global_id");
    auto real_url_param = req.get_param_value("real_url");

    TaskType task_type = StrToTaskType(task_type_str);
    if (task_type == TaskType::Unknown || target_global_id_str.empty() || real_url_param.empty()) {
        res.status = 400;
        res.set_content("Missing or invalid taskid, target_global_id, or real_url parameter", "text/plain");
        return;
    }

    boost::uuids::string_generator gen;
    DeviceID target_device_id;
    try {
        target_device_id = gen(target_global_id_str);
    } catch (const std::exception &) {
        res.status = 400;
        res.set_content("invalid target_global_id", "text/plain");
        return;
    }

    auto srv_info_opt = Docker_scheduler::getOrCrtSrvByTTypeOnDevice(task_type, target_device_id);
    if (srv_info_opt == nullopt) {
        res.status = 400;
        res.set_content("failed to get or create target service on the selected node", "text/plain");
        return;
    }

    time_record_schedule.endRecord();
    ForwardMultipartRequest(req, res, task_type_str, real_url_param, srv_info_opt.value(), time_record, time_record_schedule);
}

void HttpServer::HandleRegisterNode(const httplib::Request &req, httplib::Response &res) {
    nlohmann::json jsonData = nlohmann::json::parse(req.body);
    Device device;
    device.parseJson(jsonData);
    Docker_scheduler::RegisNode(device);
    res.status = 200;
    res.set_content("Node registered successfully", "text/plain");
    spdlog::info("Node registered successfully, param:{}", req.body);
}

void HttpServer::HandleHotStart(const httplib::Request &req, httplib::Response &res) {
    auto task_type_str = req.get_param_value("taskid");
    TaskType task_type = StrToTaskType(task_type_str);
    bool ret = Docker_scheduler::HotStartAllNodeByTType(task_type);
    if (ret) {
        res.set_content("HotStart successfully", "text/plain");
        spdlog::info("HotStart successfully, task_type:{}", to_string(nlohmann::json(task_type)));
        res.status = 200;
    }else {
        res.set_content("HotStart failed", "text/plain");
        spdlog::info("HotStart failed, task_type:{}", to_string(nlohmann::json(task_type)));
        res.status = 400;
    }
}

void HttpServer::HandleClusterResources(const httplib::Request &req, httplib::Response &res) {
    (void) req;
    spdlog::info("Fetch cluster resources");
    json response;
    response["status"] = "success";
    response["result"] = Docker_scheduler::getClusterResources();

    res.status = 200;
    res.set_content(response.dump(), "application/json");
}

void HttpServer::HandleScheduleTask(const httplib::Request &req, httplib::Response &res) {
    auto task_type_str = req.get_param_value("taskid");
    TaskType task_type;
    try {
        task_type = StrToTaskType(task_type_str);
    } catch (const std::invalid_argument &) {
        res.status = 400;
        res.set_content("invalid format taskid", "text/plain");
        return;
    }

    if (task_type == TaskType::Unknown) {
        res.status = 400;
        res.set_content("Missing or invalid taskid parameter", "text/plain");
        return;
    }

    auto device_opt = Docker_scheduler::scheduleDeviceByTType(task_type);
    if (!device_opt.has_value()) {
        res.status = 400;
        res.set_content("failed to schedule a target device", "text/plain");
        return;
    }

    const auto &device = device_opt.value();
    json response;
    response["status"] = "success";
    response["result"] = {
        {"task_type", task_type_str},
        {"target_global_id", boost::uuids::to_string(device.global_id)},
        {"type", json(device.type)},
        {"ip_address", device.ip_address},
        {"agent_port", device.agent_port}
    };
    res.status = 200;
    res.set_content(response.dump(), "application/json");
    spdlog::info("Scheduled task successfully, task_type:{}, target_global_id:{}, ip:{}",
                 task_type_str,
                 boost::uuids::to_string(device.global_id),
                 device.ip_address);
}
