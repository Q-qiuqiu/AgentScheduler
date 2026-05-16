#ifndef DOCKER_SCHEDULER_H
#define DOCKER_SCHEDULER_H

#include <string>
#include <map>
#include <set>
#include <vector>
#include <list>
#include <queue>
#include <httplib.h>
#include <nlohmann/json.hpp>
#include <mutex>
#include <shared_mutex>
#include "device.h"
#include <optional>
#include "spdlog/spdlog.h"
#include "z3++.h"
#include "TimeRecorder.h"
using namespace std;
using json = nlohmann::json;
using namespace z3;
#pragma once

struct StaticInfoItem {
    ImageInfo imageInfo;
    TaskOverhead taskOverhead;
    StaticInfoItem(){};
    StaticInfoItem(const json &device) {
        imageInfo.container_name = device["imageInfo"]["container_name"];
        imageInfo.image = device["imageInfo"]["image"];
        imageInfo.cmds = device["imageInfo"]["cmds"].get<std::vector<std::string> >();
        imageInfo.args = device["imageInfo"]["args"].get<std::vector<std::string> >();
        imageInfo.host_config_privileged = device["imageInfo"]["host_config_privileged"];
        imageInfo.env = device["imageInfo"]["env"].get<std::vector<std::string> >();
        imageInfo.host_config_binds = device["imageInfo"]["host_config_binds"].get<std::vector<std::string> >();
        imageInfo.devices = device["imageInfo"]["devices"].get<std::vector<std::string> >();
        imageInfo.host_ip = device["imageInfo"]["host_ip"];
        imageInfo.host_port = device["imageInfo"]["host_port"];
        imageInfo.container_port = device["imageInfo"]["container_port"];
        imageInfo.has_tty = device["imageInfo"]["has_tty"];

        taskOverhead.proc_time = device["taskOverhead"]["proc_time"];
        taskOverhead.mem_usage = device["taskOverhead"]["mem_usage"];
        taskOverhead.cpu_usage = device["taskOverhead"]["cpu_usage"];
        taskOverhead.xpu_usage = device["taskOverhead"]["xpu_usage"];
    }
};



class Docker_scheduler {
private:
    static std::map<TaskType, std::map<DeviceType, StaticInfoItem> > static_info; // static task info

    static std::shared_mutex devs_mutex; //
    static std::map<DeviceID, Device> device_static_info; // static device info

    static std::map<DeviceID, DeviceStatus> device_status; // dynamic device info
    static std::map<DeviceID, int> device_info_failure_count;

    // static std::shared_mutex td_map_mutex_; // Thread-safe mutex for TDMap
    static std::map<TaskType, std::map<DeviceID, DevSrvInfos> > tdMap;

    //  dynamic device info unorder_map becaues of uuid_t cant compare for the need of map

    int scheduling_trget; // current scheduling_target

    static  z3::context c;
    static std::mutex z3_mutex;  // 专门保护Z3 context
    //onnx
    // static Ort::Env env;
    // static Ort::Session* onnx_session;  // 使用指针避免初始化时构造
    static bool is_model_loaded;  // 标记模型是否已加载

public:
    Docker_scheduler();

    /// @brief read profiling result from knowledge_file and initilize
    /// @param knowledge_file name of the file
    explicit Docker_scheduler(string knowledge_file);

    // func of device_static_info
    static vector<TaskType> getTaskTypesByDeviceType(DeviceType devType);

    static void RemoveDevice(DeviceID global_id);

    /// @brief Add a new node in the cluster with its type, ip address, and agent port
    /// @param node_type device type, RK3588, ATLAS, or ORIN
    /// @param IP the IP address of the new node
    /// @param port the agent port of the new node
    /// @return the global id of the new node
    static int RegisNode(const Device &device);


    static void display_dev();

    static void display_devinfo();

    /// @brief init scheduler
    /// @param filepath profiling file path
    static void init(string filepath);

    // read file to static_Info
    static void loadStaticInfo(std::string filepath);
    // get static_info
    static std::map<TaskType, std::map<DeviceType, StaticInfoItem>> getStaticInfo() ;
    static json getClusterResources();
    static std::optional<Device> scheduleDeviceByTType(TaskType ttype);
    static std::optional<Device> scheduleDeviceByTType(
        TaskType ttype,
        const std::set<std::string> &exclude_nodes,
        const std::set<std::string> &require_nodes
    );
    static std::optional<SrvInfo> startSubAgentOnDevice(const std::string &agent_name, const DeviceID &device_id);
    static std::optional<Device> getDeviceById(const DeviceID &device_id);
    static bool deviceSupportsTask(const DeviceID &device_id, TaskType ttype);

    static ImageInfo getImage(TaskType taskType, DeviceType devType);

    /// @brief Thread-safe method to remove a device
    /// @param global_id The global ID of the device to be removed
    static void RemoveDevice(int global_id);


    static bool HotStartAllNodeByTType(TaskType ttype);

    static void startDeviceInfoCollection();

    /// @brief route a srvinfo for a quest with a specific task type
    /// @param TaskType ttype
    /// @return Selected SrvInfo
    static std::optional<SrvInfo> getOrCrtSrvByTType(TaskType ttype);
    static std::optional<SrvInfo> getOrCrtSrvByTTypeOnDevice(TaskType ttype, const DeviceID &device_id);

    // create a new  container on a specific device
    static std::optional<SrvInfo> createContainerByTType(TaskType ttype, const Device &dev);

    /// @brief select a dev when creating a new container or deal a quest
    static Device getTgtDevByTtype(TaskType ttype);

    static Device getTgtDevByTtypeAndDevIds(TaskType ttype);

    static Device getTgtDevByTtypeAndDevIds(TaskType ttype, vector<DeviceID> devIds);


    /// @brief remove inactive container
    static void inactiveTimeCallback(TaskType ttype, Device dev, string container_id);

    /// @brief Get target device ID for new coming task with type Ttype
    /// @param Ttype the type of target task 
    /// @return target device
    static Device Z3_schedule(TaskType Ttype);
    static Device Z3_schedule_v2(TaskType Ttype);
    static Device Z3_simulate_schedule(TaskType Ttype,float prob1, float prob2, float prob3);
    static Device Model_predict(TaskType Ttype);
    // 模型加载函数
    static void loadModel(const std::string& model_path);
    static int encodeTaskType(TaskType Ttype);
    static int encodePlatform(DeviceType dtype);
    void display_devstatus(DeviceID dev_id){
        DeviceStatus status = device_status[dev_id];
        status.show();
    }

    void updateStatus(DeviceID id,DeviceStatus status){
        device_status[id].cpu_used+=status.cpu_used;
        device_status[id].mem_used+=status.mem_used;
        device_status[id].xpu_used+=status.xpu_used;
    }
    void regissrv(DeviceID id,TaskType ttype){
        if(tdMap[ttype][id].dev_srv_info_status == NoExist){
            tdMap[ttype][id].dev_srv_info_status = Running;
        }
    }


};

#endif
