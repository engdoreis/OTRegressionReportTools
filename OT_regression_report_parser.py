import time
import pandas as pd
from datetime import datetime
import requests
import re
import matplotlib.pyplot as plt

pd.options.display.float_format = '{:,.2f}'.format

class Github:
  def __init__(self, user, repo):
    self.github_url = 'https://github.com'
    self.base_url=f'{self.github_url}/{user}/{repo}'
    self.throttling_limit = 2
    self.throttling = 0

  def load(self, names):
    url = self.base_url + f'/issues?q=is%3Aissue+{names[0]}'
    for name in names[1:]:
      url += f'+OR+{name}'

    r = requests.get(url + '+is%3Aopen')
    self.opened_issues_page = r.text
    r = requests.get(url + '+is%3Aclosed')
    self.closed_issues_page = r.text

  def get_issues(self, pattern, by='name', opened=True):
    if by == 'name':
      html = self.opened_issues_page if opened else self.closed_issues_page
      res = re.findall(r'"Link to Issue\.\s(.*' + pattern + r'.*)"\shref="([\w\/\d]+)">', html)
    else:
      url = self.base_url + '/issues?q=is%3Aissue' + f'+{pattern}' + ('+is%3Aopen' if opened else '+is%3Aclosed')
      r = requests.get(url)
      if r.status_code != 200:
        return [{'name': 'error', 'number': r.status_code,'link' : f'https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{r.status_code}'}]
      res = re.findall(r'"Link to Issue\.\s(.*)"\shref="([\w\/\d]+)">', r.text)

    if res:
      return [{'name': issue[0], 'number': issue[1].split('/')[-1],'link' : f'{self.github_url}/{issue[1]}'} for issue in res]
    return None

class RegressionReportParser:
  def __init__(self, cfg):
    self.cfg = cfg
    self.pages = self.download_reports_pages(self.cfg.base_url)
    self.test_list = None
   
  def get_github_revision(self, html):
    res = re.search('GitHub Revision:.*<code>([a-fA-F0-9]+)<\/code>', html)
    return res.group(1) if res else 0

  def get_build_seed(self, html):
    res = re.search('--build-seed\s([a-fA-F0-9]+)', html)
    return res.group(1) if res else 0

  def parse_reports_links(self, html):
    return re.findall('\d{4}.\d{2}.\d{2}_\d{2}.\d{2}.\d{2}/report.html', html)

  def parse_report_datetime(self, html):
    return datetime.strptime(re.findall('\w+ (\w+ \d+ \d+ \d+:\d+:\d+) UTC', html)[0], '%B %d %Y %H:%M:%S')

  def download_reports_pages(self, base_url):
      home_url = base_url + 'latest/report.html'
      pages = []
      while len(pages) < self.cfg.max_reports:
        r = requests.get(home_url)
        pages.append({'url' : home_url, 'html' : r.text})
        # Read the links for old reports.
        previous_reports = self.parse_reports_links(r.text)
        # Donwload the page for each of the old reports.
        for rep in previous_reports:
          url = base_url + rep
          pages.append({'url' : url, 'html' : requests.get(url).text})
        # Set the last of the previous reports as the new home page.
        home_url = base_url + previous_reports[-1]
      return pages[0:self.cfg.max_reports] 

  def parse_results_report(self, html, url):
    df = pd.read_html(html, decimal='.')[0]    # Read Test results table.
    df = df [['Name', 'Tests', 'Pass Rate']]   # Filter out the other columns.
    df = df[df['Tests'] != 'TOTAL']            # Filter out rows containing TOTAL.
    # While there's NaN fill it with the next row.
    while df['Name'].isnull().values.any():
      df['Name'] = df['Name'].fillna(df['Name'].shift(1))
    
    if self.cfg.add_build_data:
      df = df.append({'Tests':'Github revision' , 'Name':'Github revision', 'Pass Rate':self.get_github_revision(html)}, ignore_index=True)
      df = df.append({'Tests':'Build-seed' , 'Name':'Build-seed', 'Pass Rate':self.get_build_seed(html)}, ignore_index=True)
      
    # Rename Pass Rate column with the date of the report.
    df.rename(columns={'Pass Rate': self.parse_report_datetime(html), 'Name' : 'Suite' }, inplace=True)
    return df

  def parse_progress_report(self, html, url):
    df = pd.read_html(html, decimal='.')[1]   # Read Test plan progress table.
    df = df [['Items','Progress']]            # Filter out the other columns.
    # Rename Pass Rate column with the date of the report.
    df.rename(columns={'Progress': url.split('/')[-2].split('_')[0]}, inplace=True)
    return df

  def generate_testplan_progress_report(self):
    # Load the data of the latest report.
    df = self.parse_progress_report(self.pages[0]['html'], self.pages[0]['url'])
    # Iterate thought the previous reports merging the results.
    for page in self.pages[1:]:
      df = df.merge(self.parse_progress_report(page['html'], page['url']), on=['Items'], how='left',\
                    suffixes=['', '_clone']) # Suffixes for when there's more than one report in the same day  
    df = df.drop(df.filter(regex='_clone').columns, axis=1).set_index('Items')
    return df

  def to_float(self, v):
    if re.search('--|N/A', v):
      return 101
    elif re.search('\d?\d?\d\.\d\d', v):
      return float(v)
    elif re.search('[a-f0-9]{6,10}', v):
      return -1
    print(f'New value format:{v}')
    return-1 

  # Try to convert the report data frame column into float while handling non numeric values.
  def custom_sort(self, column):
    return column.apply(lambda v: self.to_float(v))

  def generate_test_results_report(self, by=None):
    # Load the data of the latest report.
    df = self.parse_results_report(self.pages[0]['html'], self.pages[0]['url'])
    # Iterate thought the previous reports merging the results.
    for page in self.pages[1:]:
      df = df.merge(self.parse_results_report(page['html'], page['url']), on=['Suite', 'Tests'], how='left',\
                    suffixes=['','_clone']) # Suffixes for when there's more than one report in the same day
    
    df = df.drop(df.filter(regex='_clone').columns, axis=1)

    if len(self.cfg.black_list_days) > 0:
      df = df.drop( [col for col in df.columns if any(x in str(col) for x in self.cfg.black_list_days)], axis=1)

    # Tag test that didn't exist in previous execution.
    df = df.fillna('--')
    self.test_list = df['Tests'].unique().tolist()
    self.test_list.sort()

    # Group the tests with the same result and merge the suite names.
    group_columns = list(df.columns)
    group_columns.remove('Suite')
    df = df.groupby(by=group_columns)['Suite'].apply(lambda v: '%s' % ', '.join(v)).reset_index()

    if not self.cfg.invert_columns_order:
      group_columns.reverse()
      df = df[group_columns]
      group_columns.reverse()

    if by:
      return df[df['Tests'].str.contains(by.strip())]

    group_columns.remove('Tests')
    if self.cfg.hide_healthy_tests:
      # Filter out all lines with 100% success over the last `threshold` executions.
      df = df.set_index('Tests')
      # df = df[df.iloc[:,0:self.cfg.healthy_executions_threshold].ne('100.00', axis=0).any(1)].reset_index()
      df = df[df[group_columns[0:self.cfg.healthy_executions_threshold]].ne('100.00', axis=0).any(1)].reset_index()

    if self.cfg.hide_empty_tests:
      df = df.set_index('Tests')
      df = df[df[group_columns[0]] != '--'].reset_index()

    if self.cfg.sort_tests_by_failure:
      # Sort with the order:failures in the recente executions, not executed.
      df.sort_values(by=group_columns, key=lambda x: self.custom_sort(x), inplace=True)

    # Try to find possible github issues linked.
    def parse_github_issue(git, name):
      if name in ['Build-seed', 'Github revision']:
        return ' ', ' '
      issue_parser = lambda issues: ' '.join('<a href ="{}">#{}</a>'.format(issue['link'],issue['number']) for issue in issues) if issues else ' '

      opened_links = issue_parser(git.get_issues(name, by='name'))
      closed_links = issue_parser(git.get_issues(name, by='name', opened=False))
      if opened_links != ' ' or closed_links != ' ':
        return opened_links, closed_links

      opened_links = issue_parser(git.get_issues(name, by='content'))
      closed_links = issue_parser(git.get_issues(name, by='content', opened=False))
      return opened_links, closed_links

    if self.cfg.attach_github_issues:
      git = Github('lowrisc', 'opentitan')
      git.load(df['Tests'].values.flatten().tolist())
      issues_col, closed_issues_col = zip(*df['Tests'].apply(lambda v: parse_github_issue(git, v)))
      df.insert(1, 'Issues', issues_col)
      df['Closed Issues'] =  closed_issues_col

    return df.reset_index(drop=True)

  def report_formating(self, value):
    format = ' '
    formating = lambda background, foreground: "background: {};color: {}"\
              .format(background, foreground if self.cfg.hide_passrate else "white")
    try:
      if float(value) == 0:
        format = formating("darkred", "darkred")
      elif float(value) == 100:
        format = formating("darkgreen", "darkgreen")
      elif float(value) < 100:
        format = formating("darkgoldenrod", "darkgoldenrod")
    finally:
      return format

  def format_dataframe(self, df):
    return df.style.applymap(self.report_formating)\
    .set_table_styles([{
        "selector": "td, th", "props": [("border", "1px solid grey")]
        }])
    # .set_properties(**{'text-align': 'center'})

  def parse_uvm_errors(self, html):
    res = re.findall('(UVM_ERROR).+:\s\((\w+\.\w+:\d+)', html)
    return [f'{t[0]} *** {t[1]}' for t in res]

  def generate_uvm_errors_report(self):
    df = pd.DataFrame(data = {'Error': self.parse_uvm_errors(self.pages[0]['html'])})
    df = df.groupby('Error')['Error'].count().reset_index(name='count')
    df.sort_values(by='count', ascending=False, inplace=True)
    df['Error%'] = df['count'] * 100 / df['count'].sum()
    df['cumError%'] = df['Error%'].cumsum()
    return df

  def parse_errors(self, html):
    res = re.findall('<code>(.*)<\/code>\shas\s(\d+)\sfailures:', html)
    return [{'Error': t[0], 'count' : int(t[1])} for t in res]

  def generate_errors_report(self):
    df = pd.DataFrame(data = self.parse_errors(self.pages[0]['html']))
    df['Error'] = df['Error'].apply(lambda x : re.sub('@\d+','', x))
    df = df.groupby('Error')['count'].sum().reset_index()
    df = df.sort_values(by='count', ascending=False).reset_index(drop=True)
    df['Error%'] = df['count'] * 100 / df['count'].sum()
    df['cumError%'] = df['Error%'].cumsum()
    df = df[['count','Error%','cumError%','Error']]
    # df['Error'] = df['Error'].apply(lambda x: re.search('(:?\w+\.(:?sv|c):\d+)|(:?Offending.*)|(:?Exit reason:.*)', x).group(0))
    return df
  
  def generate_tests_rampup(self, test_report):
    df = test_report.copy(deep=True)
    df = df.iloc[:,1:].apply(lambda x: x.str.contains('\d+').sum(), axis=0).reset_index()
    df.columns = ['Date', 'Count']
    df['Date'] = df['Date'].apply(lambda x: pd.np.nan if type(x) == str else x)
    return df[~df.Date.isnull()]

if __name__ == '__main__':
  git = Github('lowrisc', 'opentitan')
  git.load(['chip_sw_power_idle_load', 'chip_sw_power_sleep_load'])
  print(git.get_issues('rom_e2e_keymgr_init', by='content'))
  print(git.get_issues('rom_e2e_keymgr_init', by='content', opened=False))