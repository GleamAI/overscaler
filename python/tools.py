import subprocess
import requests
import time
from ast import literal_eval
import pandas as pd
import json
import operator
import pykube
from time import gmtime, strftime
import re

def replace_all(text, dic):
    for i, j in dic.items():
        text = text.replace(i, j)
    return text

def get_cluster_labels():

    max_nodes = 0
    min_nodes= 0

    bash_describe = "gcloud container clusters describe --format=json cluster-gleam --zone europe-west2-a"

    output = str(subprocess.check_output(bash_describe, shell=True))
    output = json.loads(output.replace("\\n", "").replace("b\'", "").replace("\'", ""))

    autoscale = output["nodePools"][0]["autoscaling"]["enabled"]
    metrics = []
    rules=[]
    if len(list(filter(re.compile("metric_.*").search, list(output["resourceLabels"].keys())))) > 0:
        for i in list(filter(re.compile("metric_.*").search, list(output["resourceLabels"].keys()))):
            metric = output["resourceLabels"][i]
            metrics.append(metric)
    overscaler = output["resourceLabels"]["overscaler"]
    if overscaler == "true":
        if len(list(filter(re.compile("rule_.*").search, list(output["resourceLabels"].keys())))) > 0:
            for i in list(filter(re.compile("rule_.*").search, list(output["resourceLabels"].keys()))):
                rule = output["resourceLabels"][i]
                rules.append(rule)

    if str(autoscale) == "True":
        max_nodes = output["nodePools"][0]["autoscaling"]["maxNodeCount"]
        min_nodes = output["nodePools"][0]["autoscaling"]["minNodeCount"]

    return autoscale, max_nodes,min_nodes, overscaler, metrics, rules

def get_num_nodes():
    return len(requests.get('http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/nodes/').json())

def cluster_auth():
    bash_auth = "gcloud container clusters get-credentials cluster-gleam --zone europe-west2-a --project gleam-ai1"
    subprocess.check_output(['bash', '-c', bash_auth])

def start_proxy():
    bash_proxy = "kubectl proxy &"
    subprocess.call(['bash', '-c', bash_proxy])

    time.sleep(5)

def get_nodes_status(metrics,standart_metrics):
    Nodes = requests.get('http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/nodes/').json()
    df_node_status = pd.DataFrame()
    for i in range(len(Nodes)):
        node_status={}
        memory_allocatable = requests.get(
            'http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/nodes/' + Nodes[
                i] + '/metrics/memory/node_allocatable').json()
        cpu_allocatable = requests.get(
            'http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/nodes/' + Nodes[
                i] + '/metrics/cpu/node_allocatable').json()
        for j in metrics:
            if j == "memory_usage_percent":
                memory_usage = requests.get('http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/nodes/'+Nodes[i]+'/metrics/memory/working_set').json()
                node_status[j] = round(memory_usage["metrics"][0]["value"]/memory_allocatable["metrics"][0]["value"]*100,2)
            elif j == "cpu_usage_percent":
                cpu_usage = requests.get('http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/nodes/'+Nodes[i]+'/metrics/cpu/usage_rate').json()
                node_status[j] = round(cpu_usage["metrics"][0]["value"]/cpu_allocatable["metrics"][0]["value"]*100,2)
            elif j in standart_metrics.keys():
                node_status[j] = requests.get('http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/nodes/'+Nodes[i]+'/metrics/'+str(standart_metrics[j])).json()["metrics"][0]["value"]
        df_aux = pd.DataFrame({'name':str(Nodes[i]),'status':[node_status],'cpu_allocatable':cpu_allocatable["metrics"][0]["value"],'memory_allocatable':memory_allocatable["metrics"][0]["value"]}, index=[i])
        df_node_status=pd.concat([df_node_status,df_aux])
    return df_node_status

def get_pods_status(node_name, memory_allocatable, cpu_allocatable, statefulset_labels,standart_metrics):
    Pods = str(
        subprocess.check_output("kubectl describe nodes "+str(node_name)+" | grep \"default \"",
                                 shell=True)).split("b'", 1)[1].split()

    df_pods_status = pd.DataFrame()
    for i in range(int(len(Pods)/10)):
        metrics= statefulset_labels.loc[statefulset_labels['name'] == str(str(Pods[i * 10 + 1]).rsplit("-",1)[0])]['metrics']
        if not metrics.empty:
            pod_status = {}
            for j in metrics[0]:
                if j == "memory_usage_percent":
                    memory_usage = requests.get("http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/namespaces/default/pods/" + str(
                            Pods[i * 10 + 1]) + "/metrics/memory/working_set").json()
                    pod_status[j] = round(
                    memory_usage["metrics"][0]["value"] / memory_allocatable * 100, 2)
                elif j == "cpu_usage_percent":
                    cpu_usage = requests.get(
                        "http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/namespaces/default/pods/" + str(
                            Pods[i * 10 + 1]) + "/metrics/cpu/usage_rate").json()
                    pod_status[j] = round(
                        cpu_usage["metrics"][0]["value"] / cpu_allocatable * 100, 2)
                elif j in standart_metrics.keys():
                    get_metric=requests.get(
                        "http://localhost:8001/api/v1/namespaces/kube-system/services/heapster/proxy/api/v1/model/namespaces/default/pods/" + str(
                            Pods[i * 10 + 1]) + "/metrics/"+str(standart_metrics[j])).json()
                    if len(get_metric["metrics"])!=0:
                        pod_status[j] =get_metric["metrics"][0]["value"]
            df_aux = pd.DataFrame({'node':str(node_name),'pod':str(Pods[i * 10 + 1]),'status':[pod_status]}, index=[i])
            df_pods_status = pd.concat([df_pods_status, df_aux])
    return df_pods_status

def get_statefulset_labels(api,namespace):
    pre_set = pykube.StatefulSet.objects(api).filter(namespace=namespace)
    df_labels = pd.DataFrame()
    index=0
    metrics = []
    rules = []

    for s in pre_set.response['items']:
        try:
            name = s["metadata"]["name"]
            if len(list(filter(re.compile("metric_.*").search,
                               list(s["metadata"]["labels"].keys())))) > 0:
                for i in list(filter(re.compile("metric_.*").search, list(s["metadata"]["labels"].keys()))):
                    metric = s["metadata"]["labels"][i]
                    metrics.append(metric)
            overscaler = s["metadata"]["labels"]["overscaler"]
            if overscaler == "true":
                overscaler = s["metadata"]["labels"]["overscaler"]
                current_count = s["metadata"]["labels"]["current_count"]
                autoscaler_count = s["metadata"]["labels"]["autoscaler_count"]
                max_replicas = s["metadata"]["labels"]["max_replicas"]
                min_replicas = s["metadata"]["labels"]["min_replicas"]

                if len(list(filter(re.compile("rule_.*").search, list(s["metadata"]["labels"].keys())))) > 0:
                    for i in list(filter(re.compile("rule_.*").search, list(s["metadata"]["labels"].keys()))):
                        rule = s["metadata"]["labels"][i]
                        rules.append(rule)
                df_aux = pd.DataFrame(
                    {'name': str(name),'overscaler':overscaler,'current_count':current_count,'autoscaler_count':autoscaler_count,'max_replicas':max_replicas,'min_replicas':min_replicas, 'metrics': [metrics],'rules':[rules]},index=[index])
                df_labels=pd.concat([df_labels,df_aux])
                index+=index
        except:
            print(strftime("%Y-%m-%d %H:%M:%S", gmtime())+"[ERROR] Error to get labels for %s" % (s["metadata"]["name"]))
    return df_labels

def print_node_status(df_node_status):


    print(strftime("%Y-%m-%d %H:%M:%S", gmtime())+" [STATUS_INFO] Node Status:")
    for i in range(len(df_node_status)):
        node_status=df_node_status.loc[i,'status']
        for j in node_status.keys():
            print(str(strftime("%Y-%m-%d %H:%M:%S", gmtime()))+" [STATUS_NODE] Node " +str(df_node_status.iloc[i,:]['name']+ " " +str(j)+" : " +str(node_status[j])))


def print_pods_status(df_pods_status):

    print(strftime("%Y-%m-%d %H:%M:%S", gmtime())+" [STATUS_INFO] Pods Status:")
    for i in range(len(df_pods_status)):
        pod_status=df_pods_status.loc[i,'status']
        for j in pod_status.keys():
            print(str(strftime("%Y-%m-%d %H:%M:%S", gmtime()))+" [STATUS_POD] Node " +str(df_pods_status.iloc[i,:]['node']+ " Pod " +str(df_pods_status.iloc[i,:]['pod']+ " " +str(j)+" : " +str(pod_status[j]))))
