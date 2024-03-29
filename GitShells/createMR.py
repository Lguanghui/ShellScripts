#  Copyright (c) 2023, Guanghui Liang. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""
required packages:
    pip3 install python-gitlab
    pip3 install gitpython
"""

import getopt
import getpass
import os
import queue
import re
import sys
import time
import git
import gitlab
import configparser
import sendFeishuBotMessage
import config_handler
import shutil
from loadingAnimation import LoadingAnimation
from makeQuestion import make_question
from MergeRequestURLFetchThread import MergeRequestURLFetchThread
from Utils import debugPrint, update_debug_mode, get_mr_url_from_local_log, MergeRequestInfo, print_step, \
    search_file_path
from gitlab.v4.objects.projects import Project
from gitlab.v4.objects import ProjectMergeRequest
from pathlib import Path
from commit_helper import CommitHelper
from Utils import get_root_path
from config_handler import MergeRequestConfigModel

PODFILE = 'Podfile'
COMMIT_CONFIRM_PROMPT = '''
请确认将要用于生成 merge request 的提交:
    message: {message}
    author: {author}
    authored_date: {authored_date}
'''


class MRHelper:
    def __init__(self):
        self.gitlab = gitlab.Gitlab.from_config('Keep', [get_root_path() + '/MRConfig.ini'])
        self.config_model: MergeRequestConfigModel = config_handler.get_config_model()
        self.projects: [Project] = self.gitlab.projects.list(get_all=True)
        self.repo = git.Repo(os.getcwd(), search_parent_directories=True)
        self.current_proj = self.get_gitlab_project(self.get_repo_name(self.repo))
        self.last_commit = CommitHelper.get_last_commit(self.repo)
        self.mr_fetcher_threads: [MergeRequestURLFetchThread] = []
        self.queue = queue.Queue()

    @classmethod
    def get_repo_name(cls, repo: git.Repo) -> str:
        url_name = repo.remotes.origin.url.split('.git')[0].split('/')[-1]
        if len(url_name) > 0:
            debugPrint(f"从仓库 url 中获取到仓库名字: {url_name}")
            return url_name
        else:
            local_name = repo.working_tree_dir.split('/')[-1]
            debugPrint(f"从仓库 url 中没有获取到仓库名字，返回本地文件夹名: {local_name}")
            return local_name

    def get_relative_mr(self, repo_url: str, commit: str) -> str | None:
        repo_name = repo_url.split('.git')[0].split('/')[-1]
        proj = self.get_gitlab_project(repo_name)
        mr_list = proj.mergerequests.list(state='merged', order_by='updated_at', get_all=True)
        for mr in mr_list:
            commit_list = [commit.id for commit in mr.commits()]
            if commit in commit_list:
                return mr.web_url

        # 没有找到对应的 MR，则直接返回 commit 对应的链接
        return proj.commits.get(commit).web_url

    def get_mr_state(self, mr_url: str) -> str:
        """
        获取指定 merge request 的状态
        :param mr_url: merge request url
        :return: 状态字符串：opened, closed, locked, merged
        """
        mr_id = mr_url.split('/')[-1]
        mr = self.current_proj.mergerequests.get(mr_id)
        return mr.state

    @classmethod
    def get_formatted_time(cls, seconds) -> str:
        return time.strftime('%a, %d %b %Y %H:%M', time.localtime(seconds))

    @classmethod
    def get_commit_and_name_from_changed_line(cls, changed_line: str) -> (str, str):
        """
        从 changed_line 里获取组件库名称以及 commit hash
        :param changed_line: 从 git 中获取到的变更行
        :return: 仓库名称，commit hash
        """
        repo_name = ''
        commit_hash = ''
        processed_line = re.sub('\s+', '', changed_line).replace('\'', '"')  # 去掉空格，替换引号，方便提取
        if "commit" in processed_line:
            # podfile 标准写法处理。 pod "...", :git => "...", :commit => "..."
            commit_re_result: [str] = re.findall(r":commit=>\"(.+?)\"", processed_line)
            url_re_result: [str] = re.findall(r":git=>\"(.+?)\"", processed_line)
            if len(commit_re_result) and len(url_re_result):
                repo_name = url_re_result[0].split('.git')[0].split('/')[-1]
                commit_hash = commit_re_result[0]
                # commit_hash = helper.get_gitlab_project(repo_name).commits.get(commit_re_result[0])
        else:
            # podfile 函数写法处理。
            # def ...
            #   "..."
            # end
            file_path = search_file_path(PODFILE)
            if len(file_path) > 0 and os.path.exists(file_path):
                commit_re_result: [str] = re.findall(r"\"(.+?)\"", processed_line)
                if len(commit_re_result) > 0:
                    commit_hash = commit_re_result[0]
                    pod_method = "METHOD_NOT_FOUND"
                    with open(file_path, 'r') as f:
                        lines: [str] = f.readlines()
                        for (index, line) in enumerate(lines):
                            if commit_hash in line and (index - 1) >= 0 and "def" in lines[index - 1]:
                                # 需要加上 strip 去掉最后的换行符
                                pod_method = lines[index - 1].replace("def", "").replace(" ", "").strip()
                                break
                    if pod_method != "METHOD_NOT_FOUND":
                        with open(file_path, 'r') as f:
                            for line in f.readlines():
                                if pod_method in line:
                                    p_line = re.sub('\s+', '', line).replace('\'', '"')
                                    url_re_result: [str] = re.findall(r":git=>\"(.+?)\"", p_line)
                                    if len(url_re_result):
                                        repo_name = url_re_result[0].split('.git')[0].split('/')[-1]
                                        break
        return repo_name, commit_hash

    def get_gitlab_project(self, keyword: str) -> Project:
        for proj in self.projects:
            # for proj in self.gitlab.projects.list(get_all=True):
            if proj.name == keyword:
                debugPrint(f"从本地已存储数组中找到 project {keyword}")
                return proj
        debugPrint(f"从本地已存储数组中没有找到 project {keyword}，重新拉取")
        return self.gitlab.projects.list(search=keyword, get_all=True)[0]

    def check_has_uncommitted_changes(self) -> bool:
        return self.repo.is_dirty(untracked_files=True)

    def addLabel(self, mr: ProjectMergeRequest, webhookUrl: str, open_id: str):
        """
        给 merge request 添加 label
        :param open_id: feishu id
        :param mr: merge request
        :param webhookUrl: 机器人 webhook
        :return:
        """

        if len(webhookUrl) == 0 or len(open_id) == 0:
            debugPrint("webhookUrl 或者 open_id 为空，不添加 label")
            return

        debugPrint("开始添加 label")
        labels = self.current_proj.labels.list()
        debugPrint(labels)

        # webhook
        webhook_labels = list(filter(lambda x: (x.name is not None) and x.name.startswith("webhook-"), labels))
        found: bool = False
        for label in webhook_labels:
            if label.description == webhookUrl:
                mr.labels.append(label.name)
                debugPrint("从已有 label 中找到合适的 webhook label")
                found = True
        if not found:
            debugPrint("创建新的 webhook label")
            label_name = 'webhook-' + str(len(webhook_labels))
            self.current_proj.labels.create({'name': label_name, 'description': webhookUrl, 'color': '#8899aa'})
            mr.labels.append(label_name)
        mr.save()

        # openid
        openid_labels = list(filter(lambda x: (x.name is not None) and x.name.startswith("id-"), labels))
        found_id = False
        for label in openid_labels:
            if label.description == open_id:
                debugPrint("从已有 label 中找到合适的 id label")
                mr.labels.append(label.name)
                found_id = True
        if not found_id:
            debugPrint("创建新的 id label")
            label_name = 'id-' + str(len(openid_labels))
            self.current_proj.labels.create({'name': label_name, 'description': open_id, 'color': '#8899aa'})
            mr.labels.append(label_name)

        mr.save()

    def create_merge_request(self):
        if self.check_has_uncommitted_changes():
            raise SystemExit('⚠️ 有未提交的更改！')
        else:
            # 确认用于生成 MR 的提交
            print(COMMIT_CONFIRM_PROMPT
                  .format(message=self.last_commit.message.strip(),
                          author=self.last_commit.author,
                          authored_date=self.get_formatted_time(self.last_commit.authored_date))
                  .rstrip())
            commit_confirm = make_question('请输入 y(回车)/n: ', ['y', 'n'])
            if commit_confirm == 'n':
                raise SystemExit('取消生成 merge request')

            # 输入目标分支
            mr_target_br = make_question('请输入 MR 目标分支（直接回车会使用默认主分支）:')
            if len(mr_target_br) == 0:
                mr_target_br = 'master' \
                    if ('origin/master' in [ref.name for ref in self.repo.remote().refs]) \
                    else 'main'
            print_step(f'目标分支: {mr_target_br}')

            # 输入 MR 标题
            mr_title = make_question('请输入 MR 标题（直接回车会使用上述提交的 message）:')
            if len(mr_title) == 0:
                mr_title = self.last_commit.message.split('\n')[0]
            print_step(f'message: {mr_title}')

            # fetch 远端改动
            LoadingAnimation.sharedInstance.showWith('fetch 远端分支改动中...',
                                                     finish_message='fetch 远端改动完成✅',
                                                     failed_message='fetch 远端改动失败❌')
            for remote in self.repo.remotes:
                remote.fetch(verbose=False)
            LoadingAnimation.sharedInstance.finished = True

            # 校验目标分支是否在远端
            mr_target_br = mr_target_br.replace('origin/', '')
            target_branch_remoted: bool = False
            for remote in self.repo.remotes:
                for ref in remote.refs:
                    if ref.name == f"origin/{mr_target_br}":
                        target_branch_remoted = True
                        break
            if not target_branch_remoted:
                raise SystemExit('⚠️ 目标分支没有 push 到远端！')

            # rebase 远端分支
            debugPrint('开始对分支进行 rebase')
            LoadingAnimation.sharedInstance.showWith('rebase 远端分支中...',
                                                     finish_message='rebase 完成✅',
                                                     failed_message='rebase 失败❌')
            os.system(f'git rebase origin/{mr_target_br} > /dev/null 2>&1')
            LoadingAnimation.sharedInstance.finished = True

            # 获取关联 MR
            LoadingAnimation.sharedInstance.showWith('处理 Podfile, 获取相关组件库 merge request 中...',
                                                     finish_message='组件库 merge request 处理完成✅',
                                                     failed_message='组件库 merge request 处理失败❌')
            debugPrint("开始处理 Podfile")
            # 获取分支 diff
            diff = CommitHelper.get_branches_file_diff(self.repo,
                                                       file_name=PODFILE,
                                                       target_branch_name=f"origin/{mr_target_br}")
            file_changed_lines: [str] = CommitHelper.get_diff_changed_lines(diff)
            debugPrint("Podfile 处理完成")
            relative_pod_mrs: [str] = []
            for line in file_changed_lines:
                line = re.sub('\s+', '', line)  # 去掉空格，方便提取
                repo_name, commit_hash = self.get_commit_and_name_from_changed_line(changed_line=line)
                if len(repo_name) and len(commit_hash):
                    debugPrint(f"获取组件库 {repo_name} project")
                    proj = self.get_gitlab_project(repo_name)
                    debugPrint(f"组件库 {repo_name} project 获取成功")
                    thread = MergeRequestURLFetchThread(proj, commit_hash=commit_hash, t_queue=self.queue)
                    self.mr_fetcher_threads.append(thread)

            for thread in self.mr_fetcher_threads:
                thread.start()

            # 等待所有线程执行
            for thread in self.mr_fetcher_threads:
                thread.join()
                debugPrint(f"线程 {thread.proj.attributes['name']} 完成")

            # 取出队列所有元素
            while not self.queue.empty():
                url = self.queue.get()
                if len(url):
                    relative_pod_mrs.append(url)

            LoadingAnimation.sharedInstance.finished = True

            description = ''
            if len(relative_pod_mrs) > 0:
                description += "<p>相关组件库提交:</p>"
            for relative_url in relative_pod_mrs:
                description += "<p>" + "    👉: " + relative_url + "</p>"
            if len(description):
                print_step('自动填写 description: ', str(description.replace('<p>', '\n').replace('</p>', '\n')))

            source_branch = self.repo.head.ref.name
            original_source_branch = source_branch
            print_step('当前分支: ', source_branch)

            # 如果当前在主分支，则切换分支
            # if source_branch in ['main', 'master', 'release', 'release_copy']:
            username = getpass.getuser()
            _time = str(int(time.time()))
            source_branch = username + '/mr' + _time
            self.repo.git.checkout('-b', source_branch)
            print_step('自动切换到分支: ', source_branch)

            print_step(f'将分支 {source_branch} push 到 remote')
            # self.repo.git.push('origin', source_branch)
            # 生成 MR。当用户对某些仓库没有管理权限时，使用 gitlab-python 内置的创建 MR 方法会失败，因此使用 shell 指令创建 MR
            cmd = f"git push " \
                  f"-o merge_request.create " \
                  f"-o merge_request.target={mr_target_br} " \
                  f"-o merge_request.title=\"{mr_title}\" " \
                  f"--set-upstream origin {source_branch} "

            log_path = os.path.join(Path.home(), "mrLog.txt")
            if os.path.exists(log_path):
                os.remove(log_path)
            os.system(f'{cmd} > {log_path} 2>&1')
            time.sleep(1)  # 等待
            LoadingAnimation.sharedInstance.showWith('获取 merge request 并修改 description 中...',
                                                     finish_message='merge request 创建完成✅',
                                                     failed_message='')
            merge_request_url = ''

            mr_info_from_local: MergeRequestInfo = get_mr_url_from_local_log(log_path)
            if len(mr_info_from_local.url) > 0 and len(mr_info_from_local.id) > 0:
                debugPrint(f"从本地 log 中拿到 merge request url: {mr_info_from_local.url}")
                merge_request_url = mr_info_from_local.url
                try:
                    merge_request: ProjectMergeRequest = self.current_proj.mergerequests.get(mr_info_from_local.id)
                    debugPrint(f"从本地 log 中拿到 url: {merge_request.web_url}, id: {mr_info_from_local.id}")
                    merge_request.description = description
                    self.addLabel(merge_request,
                                  self.config_model.feishu_bot_webhook,
                                  open_id=self.config_model.self_open_id)
                    merge_request.save()
                except Exception as err:
                    debugPrint(err)
                    debugPrint(f"使用本地 mr id {mr_info_from_local.id} 没有拿到 merge request，尝试延迟重试")
                    retry_count = 0
                    found: bool = False
                    while retry_count < 8 and not found:
                        debugPrint(f"第 {retry_count} 次尝试获取刚创建的 merge request 链接")
                        mr_list = self.current_proj.mergerequests.list(state='opened',
                                                                       order_by='updated_at',
                                                                       get_all=True)
                        for mr in mr_list:
                            mr: ProjectMergeRequest = mr
                            debugPrint(f"比对 merge request: {str(mr.web_url)}")
                            if merge_request_url == str(mr.web_url):
                                debugPrint("merge request 比对成功，修改 description")
                                mr.description = description
                                self.addLabel(mr, self.config_model.feishu_bot_webhook,
                                              open_id=self.config_model.self_open_id)
                                mr.save()
                                found = True
                                break
                        time.sleep(1)
                        retry_count += 1
            else:
                retry_count = 0
                while retry_count < 8 and len(merge_request_url) == 0:
                    debugPrint(f"第 {retry_count} 次尝试获取刚创建的 merge request 链接")
                    mr_list = self.current_proj.mergerequests.list(state='opened', order_by='updated_at', get_all=True)
                    for mr in mr_list:
                        commit_list = [commit.id for commit in mr.commits()]
                        if self.last_commit.hexsha in commit_list:
                            merge_request_url = mr.web_url
                            mr.description = description
                            self.addLabel(mr, self.config_model.feishu_bot_webhook,
                                          open_id=self.config_model.self_open_id)
                            mr.save()
                            break
                    time.sleep(1)
                    retry_count += 1

            LoadingAnimation.sharedInstance.finished = True

            print_step(f'删除本地分支 {source_branch}，并切换到原分支 {original_source_branch}')
            self.repo.git.checkout(original_source_branch)
            self.repo.delete_head(source_branch)

            # 删除 log
            try:
                if os.path.exists(log_path):
                    os.remove(log_path)
            except FileNotFoundError as file_error:
                debugPrint(f"删除本地 log 失败，文件不存在: {file_error}")

            if len(merge_request_url) > 0:
                print_step(f'merge request 创建成功，链接: \n    {merge_request_url}')
                print('')
                sendFeishuBotMessage.send_feishubot_message(merge_request_url,
                                                            author=str(self.repo.config_reader().get_value("user",
                                                                                                           "name")),
                                                            message=mr_title.strip(),
                                                            repo_name=self.get_repo_name(self.repo),
                                                            target_branch=mr_target_br,
                                                            config=self.config_model)
            else:
                raise SystemExit('merge request 创建失败！')


def get_config_new_value(key: str, section: str, config: configparser.ConfigParser):
    if key in config[section] and len(config[section][key]) > 0:
        return config[section][key]
    else:
        return ''


def create_config_file():
    config_path = get_root_path() + '/config.json'
    config_example_path = get_root_path() + '/config_example.json'
    if not os.path.exists(config_path) and os.path.exists(config_example_path):
        shutil.copyfile(config_example_path, config_path)

    path = get_root_path() + '/MRConfig.ini'
    if os.path.exists(path):
        current_config = configparser.ConfigParser()
        current_config.read(path)
        section = current_config.sections()[0]
        current_config[section]['url'] = 'https://gitlab.gotokeep.com'
        current_config[section]['private_token'] = get_config_new_value('private_token', section, current_config)
        current_config[section]['api_version'] = '4'
        with open(path, 'w') as configfile:
            current_config.write(configfile)

    else:
        f = open(get_root_path() + '/MRConfig.ini', 'w')
        f.seek(0)
        f.truncate()
        f.write("""
[Keep]
url = https://gitlab.gotokeep.com
private_token = *****
api_version = 4
            """.strip())
        f.close()
    raise SystemExit('配置文件创建成功')


if __name__ == '__main__':
    opts, args = getopt.getopt(sys.argv, "", ["--init", "--debug", "--lazy"])

    lazy_mode: bool = False

    if '--init' in args:
        # 创建配置文件
        create_config_file()
    if '--debug' in args:
        update_debug_mode(True)
        debugPrint('当前是 DEBUG 模式')
    if '--lazy' in args:
        from Utils import Colors

        print(Colors.CBOLD + Colors.CGREEN + "当前是懒人模式，自动检测并更新组件库最新 commit（7 天内）" + Colors.ENDC)
        lazy_mode = True

    # 创建 merge request
    LoadingAnimation.sharedInstance.showWith('获取仓库配置中，需要联网，请耐心等待...',
                                             finish_message='仓库配置获取完成✅', failed_message='仓库配置获取失败❌')
    try:
        helper = MRHelper()
    except Exception as e:
        LoadingAnimation.sharedInstance.failed = True
        time.sleep(0.2)
        debugPrint(e)
        raise SystemExit()
    LoadingAnimation.sharedInstance.finished = True
    if lazy_mode:
        from createMR_lazy import do_lazy_create

        do_lazy_create(helper)
    else:
        helper.create_merge_request()

    # DEBUG
    # _diff = CommitHelper.get_branches_file_diff(helper.repo,
    #                                             file_name=PODFILE,
    #                                             target_branch_name=f"script_test_dev")
    # changed_lines: [str] = CommitHelper.get_diff_changed_lines(_diff)
    # # changed_lines = CommitHelper.get_changed_lines(helper.last_commit, PODFILE)
    # for line in changed_lines:
    #     print(line)
    # relative_pod_mrs: [str] = []
    # for line in changed_lines:
    #     name, commit = MRHelper.get_commit_and_name_from_changed_line(line)
    #     print(name, commit, sep=' => ')
