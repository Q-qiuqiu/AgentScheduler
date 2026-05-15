#pragma once

#include <string>
#include <httplib.h>
#include "scheduler.h"
#include "spdlog/spdlog.h"

// 用户请求ai任务处理
const std::string QUSET_ROUTE = "/quest";
const std::string QUEST_ON_NODE_ROUTE = "/quest_on_node";
const std::string REGISTER_NODE_ROUTE = "/register_node";
const std::string HOTSTART_ROUTE = "/hot_start";
const std::string CLUSTER_RESOURCES_ROUTE = "/cluster_resources";
const std::string START_SUB_AGENT_ROUTE = "/start_sub_agent";
const std::string SCHEDULE_TASK_ROUTE = "/schedule_task";

class HttpServer {
public:
    HttpServer(std::string ip, int port,string absoulte_config_path);
    bool Start();

private:
    static void HandleQuest(const httplib::Request &req, httplib::Response &res);
    static void HandleQuestOnNode(const httplib::Request &req, httplib::Response &res);
    static void HandleRegisterNode(const httplib::Request &req, httplib::Response &res);
    static void HandleHotStart(const httplib::Request &req, httplib::Response &res);
    static void HandleClusterResources(const httplib::Request &req, httplib::Response &res);
    static void HandleScheduleTask(const httplib::Request &req, httplib::Response &res);

    std::string ip;
    int port;
};
